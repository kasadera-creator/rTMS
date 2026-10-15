"""Shared transaction boundary for treatment and mapping session writers."""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from django.db import transaction

from rtms_app.models import Patient, TreatmentCourse
from rtms_app.models import MappingSession, TreatmentSession
from rtms_app.services.patient_mutex import acquire_patient_mutex


class PatientCourseWriteError(ValueError):
    """Raised when a Patient/Course write scope is missing or ambiguous."""


def validate_session_course(session, patient, course_number, treatment_course=None):
    expected_course_id = treatment_course.pk if treatment_course is not None else None
    if (
        session.patient_id != patient.pk
        or session.course_number != course_number
        or session.treatment_course_id != expected_course_id
    ):
        raise PatientCourseWriteError(
            "SessionのPatient/Course所有関係が一致しません。"
        )
    return session


@dataclass(frozen=True)
class PatientCourseWriteScope:
    patient: Patient
    treatment_course: TreatmentCourse | None
    course_number: int

    @property
    def session_filter(self):
        if self.treatment_course is not None:
            return {"treatment_course": self.treatment_course}
        return {
            "patient": self.patient,
            "course_number": self.course_number,
            "treatment_course__isnull": True,
        }


_active_writer = ContextVar("rtms_patient_course_writer", default=None)


def _resolve_scope(
    patient: Patient,
    course_number: int,
    *,
    expected_course_id=None,
    require_course=False,
) -> PatientCourseWriteScope:
    courses = list(
        TreatmentCourse.objects.select_for_update()
        .filter(patient=patient, course_number=course_number)
        .order_by("pk")[:2]
    )
    if len(courses) > 1:
        raise PatientCourseWriteError("複数のCourse候補があるため保存できません。")

    treatment_course = courses[0] if courses else None
    if expected_course_id is not None and (
        treatment_course is None or treatment_course.pk != expected_course_id
    ):
        raise PatientCourseWriteError("指定CourseがPatientまたはCourse番号と一致しません。")
    if require_course and treatment_course is None:
        raise PatientCourseWriteError("対象の治療クールが見つかりません。")

    scope = PatientCourseWriteScope(patient, treatment_course, course_number)
    for model in (TreatmentSession, MappingSession):
        if treatment_course is None:
            mismatched_rows = model.objects.select_for_update().filter(
                patient=patient,
                course_number=course_number,
            ).exclude(treatment_course__isnull=True)
        else:
            mismatched_rows = model.objects.select_for_update().filter(
                treatment_course=treatment_course,
            ).exclude(patient=patient, course_number=course_number)
            if not mismatched_rows.exists():
                mismatched_rows = model.objects.select_for_update().filter(
                    patient=patient,
                    course_number=course_number,
                ).exclude(treatment_course=treatment_course)
        if mismatched_rows.exists():
            raise PatientCourseWriteError(
                "SessionのCourse所有関係が曖昧または不整合のため保存できません。"
            )

    return scope


@contextmanager
def patient_course_writer(
    patient_id,
    course_number=None,
    *,
    expected_course_id=None,
    require_course=False,
):
    """Serialize one Patient's Course writes in a shared outer transaction.

    The Patient mutex is the first database operation when creating the outer
    boundary. Nested writers for the same Patient reuse the held mutex and
    resolve their requested Course again inside that transaction.
    """
    active = _active_writer.get()
    if active is not None:
        active_patient_id, active_course_number, active_patient = active
        if active_patient_id != patient_id:
            raise PatientCourseWriteError("別のPatient writer境界へ入ることはできません。")
        active_patient = Patient.objects.get(pk=active_patient_id)
        resolved_number = (
            course_number if course_number is not None else active_course_number
        )
        scope = _resolve_scope(
            active_patient,
            resolved_number,
            expected_course_id=expected_course_id,
            require_course=require_course,
        )
        yield scope
        return

    with transaction.atomic():
        locked_patient = acquire_patient_mutex(patient_id)
        resolved_number = (
            course_number
            if course_number is not None else locked_patient.course_number or 1
        )
        scope = _resolve_scope(
            locked_patient,
            resolved_number,
            expected_course_id=expected_course_id,
            require_course=require_course,
        )
        token = _active_writer.set((locked_patient.pk, resolved_number, locked_patient))
        try:
            yield scope
        finally:
            _active_writer.reset(token)