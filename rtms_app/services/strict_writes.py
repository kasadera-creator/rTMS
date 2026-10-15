"""Strict Course-aware write APIs for Course-owned records."""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from rtms_app.models import (
    Assessment,
    AssessmentRecord,
    AssessmentSchedule,
    MappingSchedule,
    MappingSession,
    Patient,
    TreatmentCourse,
    TreatmentSession,
)
from rtms_app.services.patient_mutex import acquire_patient_mutex


def _course_scope(patient: Patient, treatment_course: TreatmentCourse, course_number=None):
    if treatment_course is None:
        raise ValidationError("TreatmentCourse is required for a normal Course write")
    if treatment_course.patient_id != patient.id:
        raise ValidationError("TreatmentCourse belongs to a different patient")
    if course_number is not None and treatment_course.course_number != course_number:
        raise ValidationError("TreatmentCourse and course_number do not match")
    return {
        "patient": patient,
        "treatment_course": treatment_course,
        "course_number": treatment_course.course_number,
    }


class MappingScheduleWriteError(ValueError):
    """Raised when a MappingSchedule cannot be safely scoped to one Course."""


def _resolve_mapping_course(patient, course_number):
    courses = list(
        TreatmentCourse.objects.select_for_update()
        .filter(patient=patient, course_number=course_number)
        .order_by("pk")[:2]
    )
    if len(courses) > 1:
        raise MappingScheduleWriteError("複数のCourse候補があるため位置決め予定を保存できません。")
    return courses[0] if courses else None


def _load_mapping_schedule_candidates(patient, treatment_course, course_number, week_number):
    strict_filter = Q(
        patient=patient,
        course_number=course_number,
        week_number=week_number,
        treatment_course__isnull=False,
    )
    if treatment_course is not None:
        strict_filter |= Q(
            treatment_course=treatment_course,
            week_number=week_number,
        )
    strict_schedules = list(
        MappingSchedule.objects.select_for_update().filter(strict_filter)
    )
    legacy_schedules = list(
        MappingSchedule.objects.select_for_update().filter(
            patient=patient,
            course_number=course_number,
            week_number=week_number,
            treatment_course__isnull=True,
        )
    )
    return strict_schedules, legacy_schedules


def _update_mapping_schedule_date(schedule, planned_date, *, treatment_course):
    if schedule.planned_date == planned_date:
        return
    updated_rows = MappingSchedule.objects.filter(
        pk=schedule.pk,
        patient_id=schedule.patient_id,
        course_number=schedule.course_number,
        week_number=schedule.week_number,
        treatment_course=treatment_course,
    ).update(planned_date=planned_date, updated_at=timezone.now())
    if updated_rows != 1:
        raise MappingScheduleWriteError("位置決め予定が更新中に変更されたため保存できません。")
    schedule.planned_date = planned_date


def _upsert_scoped_mapping_schedule(
    patient, treatment_course, course_number, *, week_number, planned_date,
):
    strict_schedules, legacy_schedules = _load_mapping_schedule_candidates(
        patient, treatment_course, course_number, week_number,
    )
    if len(strict_schedules) > 1 or len(legacy_schedules) > 1:
        raise MappingScheduleWriteError("位置決め予定の候補が複数あるため保存できません。")
    if strict_schedules and legacy_schedules:
        raise MappingScheduleWriteError("Course付き予定とlegacy予定が重複しているため保存できません。")

    if strict_schedules:
        schedule = strict_schedules[0]
        if (
            treatment_course is None
            or schedule.treatment_course_id != treatment_course.pk
            or schedule.patient_id != patient.pk
            or schedule.course_number != course_number
            or schedule.week_number != week_number
        ):
            raise MappingScheduleWriteError("Course付き予定の所有者またはCourse番号が一致しません。")
        _update_mapping_schedule_date(
            schedule, planned_date, treatment_course=treatment_course,
        )
        return schedule, False

    if legacy_schedules:
        schedule = legacy_schedules[0]
        if (
            schedule.treatment_course_id is not None
            or schedule.patient_id != patient.pk
            or schedule.course_number != course_number
            or schedule.week_number != week_number
        ):
            raise MappingScheduleWriteError("legacy予定の所有者またはCourse番号が一致しません。")
        if treatment_course is not None:
            updated_rows = MappingSchedule.objects.filter(
                pk=schedule.pk,
                patient_id=patient.pk,
                course_number=course_number,
                week_number=week_number,
                treatment_course__isnull=True,
            ).update(treatment_course_id=treatment_course.pk)
            if updated_rows != 1:
                raise MappingScheduleWriteError("legacy予定を安全にCourseへ紐付けできません。")
            schedule.treatment_course = treatment_course
        _update_mapping_schedule_date(
            schedule, planned_date, treatment_course=treatment_course,
        )
        return schedule, False

    schedule = MappingSchedule.objects.create(
        patient=patient,
        treatment_course=treatment_course,
        course_number=course_number,
        week_number=week_number,
        planned_date=planned_date,
    )
    return schedule, True


def create_treatment_session_strict(patient, treatment_course, *, course_number=None, **fields):
    return TreatmentSession.objects.create(
        **_course_scope(patient, treatment_course, course_number), **fields,
    )


def update_or_create_treatment_session_strict(
    patient, treatment_course, *, course_number=None, defaults=None, **lookup,
):
    scope = _course_scope(patient, treatment_course, course_number)
    values = dict(defaults or {})
    values.update(scope)
    return TreatmentSession.objects.update_or_create(
        treatment_course=treatment_course, **lookup, defaults=values,
    )


def get_or_create_treatment_session_legacy(patient, course_number, *, defaults=None, **lookup):
    values = dict(defaults or {})
    values.update({"patient": patient, "course_number": course_number, "treatment_course": None})
    return TreatmentSession.objects.get_or_create(
        patient=patient, course_number=course_number, **lookup, defaults=values,
    )


def update_or_create_treatment_session_legacy(
    patient, course_number, *, defaults=None, **lookup,
):
    values = dict(defaults or {})
    values.update({"patient": patient, "course_number": course_number, "treatment_course": None})
    return TreatmentSession.objects.update_or_create(
        patient=patient, course_number=course_number, **lookup, defaults=values,
    )


def get_or_create_treatment_session_strict(
    patient, treatment_course, *, course_number=None, defaults=None, **lookup,
):
    scope = _course_scope(patient, treatment_course, course_number)
    values = dict(defaults or {})
    values.update(scope)
    return TreatmentSession.objects.get_or_create(
        treatment_course=treatment_course, **lookup, defaults=values,
    )


def bulk_create_treatment_sessions_strict(patient, treatment_course, sessions, *, course_number=None):
    scope = _course_scope(patient, treatment_course, course_number)
    for session in sessions:
        session.patient = scope["patient"]
        session.treatment_course = scope["treatment_course"]
        session.course_number = scope["course_number"]
    return TreatmentSession.objects.bulk_create(sessions)


def bulk_create_treatment_sessions_legacy(patient, course_number, sessions):
    for session in sessions:
        session.patient = patient
        session.treatment_course = None
        session.course_number = course_number
    return TreatmentSession.objects.bulk_create(sessions)


def save_treatment_session_strict(session, patient, treatment_course, *, course_number=None):
    scope = _course_scope(patient, treatment_course, course_number)
    session.patient = scope["patient"]
    session.treatment_course = scope["treatment_course"]
    session.course_number = scope["course_number"]
    session.save()
    return session


def save_treatment_session_legacy(session, patient, course_number):
    session.patient = patient
    session.treatment_course = None
    session.course_number = course_number
    session.save()
    return session


def save_mapping_session_strict(session, patient, treatment_course, *, course_number=None):
    scope = _course_scope(patient, treatment_course, course_number)
    session.patient = scope["patient"]
    session.treatment_course = scope["treatment_course"]
    session.course_number = scope["course_number"]
    session.save()
    return session


@transaction.atomic
def update_or_create_mapping_schedule_strict(
    patient, treatment_course, *, week_number, planned_date, course_number=None,
):
    scope = _course_scope(patient, treatment_course, course_number)
    locked_patient = acquire_patient_mutex(patient.pk)
    locked_course = TreatmentCourse.objects.select_for_update().filter(
        pk=treatment_course.pk,
        patient=locked_patient,
    ).first()
    if locked_course is None or locked_course.course_number != scope["course_number"]:
        raise MappingScheduleWriteError("指定CourseがPatientまたはCourse番号と一致しません。")
    return _upsert_scoped_mapping_schedule(
        locked_patient,
        locked_course,
        locked_course.course_number,
        week_number=week_number,
        planned_date=planned_date,
    )


def update_or_create_assessment_schedule_strict(
    patient, treatment_course, *, scale, timing, planned_date, course_number=None,
):
    scope = _course_scope(patient, treatment_course, course_number)
    return AssessmentSchedule.objects.update_or_create(
        treatment_course=treatment_course,
        scale=scale,
        timing=timing,
        defaults={"patient": scope["patient"], "course_number": scope["course_number"], "planned_date": planned_date},
    )


def save_assessment_strict(instance, patient, treatment_course, *, course_number=None):
    scope = _course_scope(patient, treatment_course, course_number)
    instance.patient = scope["patient"]
    instance.treatment_course = scope["treatment_course"]
    instance.course_number = scope["course_number"]
    instance.save()
    return instance


def save_assessment_record_strict(instance, patient, treatment_course, *, course_number=None):
    scope = _course_scope(patient, treatment_course, course_number)
    instance.patient = scope["patient"]
    instance.treatment_course = scope["treatment_course"]
    instance.course_number = scope["course_number"]
    instance.save()
    return instance


def save_mapping_session_legacy(session, patient, course_number):
    session.patient = patient
    session.course_number = course_number
    session.treatment_course = None
    session.save()
    return session


@transaction.atomic
def update_or_create_mapping_schedule_legacy(
    patient, course_number, *, week_number, planned_date,
):
    """Explicitly allow a legacy write only when no matching Course exists."""
    locked_patient = acquire_patient_mutex(patient.pk)
    treatment_course = _resolve_mapping_course(locked_patient, course_number)
    return _upsert_scoped_mapping_schedule(
        locked_patient,
        treatment_course,
        course_number,
        week_number=week_number,
        planned_date=planned_date,
    )


def update_or_create_assessment_schedule_legacy(
    patient, course_number, *, scale, timing, planned_date,
):
    return AssessmentSchedule.objects.update_or_create(
        patient=patient,
        course_number=course_number,
        scale=scale,
        timing=timing,
        defaults={"planned_date": planned_date, "treatment_course": None},
    )