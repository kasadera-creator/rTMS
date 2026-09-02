"""Strict Course-aware write APIs for Course-owned records."""

from django.core.exceptions import ValidationError

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


def update_or_create_mapping_schedule_strict(
    patient, treatment_course, *, week_number, planned_date, course_number=None,
):
    scope = _course_scope(patient, treatment_course, course_number)
    return MappingSchedule.objects.update_or_create(
        treatment_course=treatment_course,
        week_number=week_number,
        defaults={"patient": scope["patient"], "course_number": scope["course_number"], "planned_date": planned_date},
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


def update_or_create_mapping_schedule_legacy(
    patient, course_number, *, week_number, planned_date,
):
    return MappingSchedule.objects.update_or_create(
        patient=patient,
        course_number=course_number,
        week_number=week_number,
        defaults={"planned_date": planned_date, "treatment_course": None},
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