import sqlite3
import time
from functools import wraps
from datetime import timedelta

from django.db import OperationalError, connection, transaction
from django.utils import timezone

from rtms_app.models import (
    InpatientPlan,
    Patient,
    ResourceAssignment,
    ResourcePool,
    RtmSWaitlistEntry,
    TreatmentCourse,
    TreatmentSession,
)
from rtms_app.services.resource_capacity import (
    ensure_schedule_capacity,
    reserve_sqlite_writer_for_capacity,
)
from rtms_app.services.rtms_treatment_reservations import (
    create_or_replace_rtms_treatment_reservation,
)
from rtms_app.services.rtms_schedule import generate_treatment_dates, get_japanese_holidays


class ActualAdmissionError(ValueError):
    """Raised when an actual admission cannot be recorded for a Course plan."""


class ActualDischargeError(ValueError):
    """Raised when an actual discharge cannot be recorded for a Course plan."""


class ActiveResourceAssignmentError(ValueError):
    """Raised when ordinary schedule confirmation targets a Course with active resources."""


class InvalidWaitlistEntryError(ValueError):
    """Raised when a Waitlist entry cannot be used for schedule confirmation."""


class ScheduleConfirmationBusyError(RuntimeError):
    """Raised when SQLite remains busy after bounded schedule-confirm retries."""


CONFIRM_SQLITE_BUSY_RETRIES = 2
CONFIRM_SQLITE_BUSY_BACKOFF_SECONDS = (0.05, 0.1)
_SQLITE_BUSY_CODES = {getattr(sqlite3, "SQLITE_BUSY", 5), getattr(sqlite3, "SQLITE_LOCKED", 6)}


def _is_sqlite_busy_error(error):
    if connection.vendor != "sqlite":
        return False

    cause = error
    while cause is not None:
        error_code = getattr(cause, "sqlite_errorcode", None)
        if error_code is not None and (error_code & 0xFF) in _SQLITE_BUSY_CODES:
            return True
        error_name = getattr(cause, "sqlite_errorname", "")
        if error_name.startswith(("SQLITE_BUSY", "SQLITE_LOCKED")):
            return True
        cause = cause.__cause__ or cause.__context__
    return False


def _sleep_before_confirmation_retry(retry_number):
    time.sleep(CONFIRM_SQLITE_BUSY_BACKOFF_SECONDS[retry_number - 1])


def _retry_sqlite_busy_confirmation(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        can_retry_whole_transaction = not connection.in_atomic_block
        for attempt in range(CONFIRM_SQLITE_BUSY_RETRIES + 1):
            try:
                with transaction.atomic():
                    reserve_sqlite_writer_for_capacity(kwargs["admission_date"])
                    return function(*args, **kwargs)
            except OperationalError as exc:
                if not _is_sqlite_busy_error(exc):
                    raise
                if not can_retry_whole_transaction or attempt == CONFIRM_SQLITE_BUSY_RETRIES:
                    raise ScheduleConfirmationBusyError(
                        "同時処理のため確定処理を完了できませんでした。もう一度お試しください。"
                    ) from exc
                _sleep_before_confirmation_retry(attempt + 1)

    return wrapped


@transaction.atomic
def record_actual_admission(*, treatment_course: TreatmentCourse, user=None):
    """Record the explicit admission event for one already-scheduled Course."""
    locked_course = TreatmentCourse.objects.select_for_update().get(pk=treatment_course.pk)
    plan = InpatientPlan.objects.select_for_update().filter(
        treatment_course=locked_course,
    ).first()
    if plan is None:
        raise ActualAdmissionError("入院予定が登録されていないため、実入院を記録できません。")
    if plan.actual_admission_at is not None:
        if plan.status != "admitted":
            raise ActualAdmissionError("実入院日時と入院計画の状態が一致しません。管理者に確認してください。")
        return {"plan": plan, "already_recorded": True}
    if plan.status not in {"provisional", "scheduled"}:
        raise ActualAdmissionError("仮予定または確定済みの日程から実入院を記録してください。")

    plan.actual_admission_at = timezone.now()
    plan.status = "admitted"
    update_fields = ["actual_admission_at", "status", "updated_at"]
    if user is not None:
        plan.updated_by = user
        update_fields.append("updated_by")
    plan.save(update_fields=update_fields)
    return {"plan": plan, "already_recorded": False}


@transaction.atomic
def record_actual_discharge(*, treatment_course: TreatmentCourse, user=None):
    """Record actual discharge for one Course without changing planned or legacy dates."""
    locked_course = TreatmentCourse.objects.select_for_update().get(pk=treatment_course.pk)
    plan = InpatientPlan.objects.select_for_update().filter(
        treatment_course=locked_course,
    ).first()
    if plan is None:
        raise ActualDischargeError("入院計画が登録されていないため、実退院を記録できません。")

    if plan.actual_discharge_at is not None:
        if plan.status != "discharged":
            raise ActualDischargeError("実退院日時と入院計画の状態が一致しません。管理者に確認してください。")
        return {"plan": plan, "already_recorded": True}
    if plan.status == "discharged":
        raise ActualDischargeError("退院済みの状態ですが実退院日時がありません。管理者に確認してください。")
    if plan.status != "admitted" or plan.actual_admission_at is None:
        raise ActualDischargeError("実入院日時と入院中の状態が確認できないため、実退院を記録できません。")

    plan.actual_discharge_at = timezone.now()
    plan.status = "discharged"
    update_fields = ["actual_discharge_at", "status", "updated_at"]
    if user is not None:
        plan.updated_by = user
        update_fields.append("updated_by")
    plan.save(update_fields=update_fields)
    return {"plan": plan, "already_recorded": False}


@transaction.atomic
def get_or_create_inpatient_plan(*, treatment_course: TreatmentCourse, user=None):
    """Return the Course's single active plan without copying legacy dates."""
    locked_course = TreatmentCourse.objects.select_for_update().get(pk=treatment_course.pk)
    plan, _created = InpatientPlan.objects.get_or_create(
        treatment_course=locked_course,
        defaults={"created_by": user, "updated_by": user},
    )
    if user is not None and plan.updated_by_id != user.pk:
        plan.updated_by = user
        plan.save(update_fields=["updated_by", "updated_at"])
    return plan


@transaction.atomic
def update_calendar_course_adjustment(
    *, treatment_course: TreatmentCourse, admission_date, treatment_start_date,
    is_all_case_survey, private_room_planned=None, planned_treatment_sessions=30,
    waitlist_entry=None, user=None,
):
    """Persist calendar controls through the Course and its existing plan."""
    locked_patient = Patient.objects.select_for_update().get(pk=treatment_course.patient_id)
    locked_course = TreatmentCourse.objects.select_for_update().get(
        pk=treatment_course.pk,
        patient=locked_patient,
    )
    has_sessions = TreatmentSession.objects.filter(treatment_course=locked_course).exists()
    if has_sessions and locked_course.first_treatment_date != treatment_start_date:
        from rtms_app.services.schedule import reschedule_treatment_start_date

        reschedule_treatment_start_date(
            locked_patient,
            treatment_start_date,
            course_number=locked_course.course_number,
            sync_reservations=False,
        )
    locked_course.admission_date = admission_date
    locked_course.first_treatment_date = treatment_start_date
    if private_room_planned is not None:
        locked_course.private_room_planned = private_room_planned
    locked_course.is_all_case_survey = is_all_case_survey
    locked_course.planned_treatment_sessions = planned_treatment_sessions
    update_fields = [
        "admission_date",
        "first_treatment_date",
        "is_all_case_survey",
        "planned_treatment_sessions",
        "updated_at",
    ]
    if private_room_planned is not None:
        update_fields.append("private_room_planned")
    locked_course.save(update_fields=update_fields)
    treatment_dates = None
    cancelled_treatment_dates = None
    if has_sessions:
        holidays = get_japanese_holidays(
            treatment_start_date, treatment_start_date + timedelta(days=365),
        )
        treatment_dates = generate_treatment_dates(
            treatment_start_date,
            total=30,
            holidays=holidays,
        )
        if len(treatment_dates) != 30 or treatment_dates[0] != treatment_start_date:
            treatment_dates = None
        else:
            cancelled_treatment_dates = list(
                TreatmentSession.objects.filter(
                    treatment_course=locked_course,
                    status="skipped",
                    session_date__in=treatment_dates,
                ).values_list("session_date", flat=True)
            )
    reservation = create_or_replace_rtms_treatment_reservation(
        treatment_course=locked_course,
        admission_date=admission_date,
        first_treatment_date=treatment_start_date,
        treatment_dates=treatment_dates,
        cancelled_treatment_dates=cancelled_treatment_dates,
        reservation_status="provisional" if waitlist_entry is not None else None,
        waitlist_entry=waitlist_entry,
        user=user,
    )
    plan = get_or_create_inpatient_plan(treatment_course=locked_course, user=user)
    plan.planned_admission_date = admission_date
    plan.updated_by = user
    plan.save(update_fields=["planned_admission_date", "updated_by", "updated_at"])
    return {"course": locked_course, "plan": plan, "reservation": reservation}


@_retry_sqlite_busy_confirmation
def confirm_inpatient_schedule(
    *,
    treatment_course: TreatmentCourse,
    resource_pool: ResourcePool,
    admission_date,
    treatment_start_date,
    room_start_date,
    room_end_date=None,
    planned_discharge_date=None,
    estimated_discharge_date=None,
    certainty="confirmed",
    user=None,
    waitlist_entry: RtmSWaitlistEntry | None = None,
):
    """Confirm plan and room allocation without implicit TreatmentSession coupling."""
    locked_course = TreatmentCourse.objects.select_for_update().get(pk=treatment_course.pk)
    locked_waitlist_entry = None
    if waitlist_entry is not None:
        locked_waitlist_entry = RtmSWaitlistEntry.objects.select_for_update().get(pk=waitlist_entry.pk)
        if locked_waitlist_entry.treatment_course_id != locked_course.pk:
            raise InvalidWaitlistEntryError("待機リストと確定対象のCourseが一致しません。")
        if locked_waitlist_entry.status not in {"waiting", "provisional"}:
            raise InvalidWaitlistEntryError("この状態の待機リストは日程確定に使用できません。")

    if ResourceAssignment.objects.filter(
        treatment_course=locked_course,
        status="active",
    ).exists():
        raise ActiveResourceAssignmentError(
            "実利用中のResourceがあるため、通常の日程確定はできません。"
        )

    locked_pool = ResourcePool.objects.select_for_update().get(pk=resource_pool.pk)
    existing_assignment = ResourceAssignment.objects.select_for_update().filter(
        treatment_course=locked_course,
        resource_pool=locked_pool,
        status__in=("planned", "active"),
    ).order_by("-pk").first()
    capacity = ensure_schedule_capacity(
        locked_pool,
        admission_date,
        room_start_date,
        room_end_date,
        exclude_assignment_id=existing_assignment.pk if existing_assignment else None,
        exclude_course_id=locked_course.pk,
    )
    plan, _created = InpatientPlan.objects.select_for_update().get_or_create(
        treatment_course=locked_course,
        defaults={"created_by": user, "updated_by": user},
    )
    reservation = None
    if plan.status not in {"cancelled", "discharged"} and plan.actual_discharge_at is None:
        reservation = create_or_replace_rtms_treatment_reservation(
            treatment_course=locked_course,
            admission_date=admission_date,
            first_treatment_date=treatment_start_date,
            reservation_status="confirmed",
            user=user,
        )
    if plan.actual_discharge_at is not None:
        plan.status = "discharged"
    elif plan.status == "cancelled":
        plan.status = "cancelled"
    elif plan.status == "admitted" or plan.actual_admission_at is not None:
        plan.status = "admitted"
    else:
        plan.status = "scheduled"
    plan.planned_admission_date = admission_date
    plan.planned_discharge_date = planned_discharge_date
    plan.estimated_discharge_date = estimated_discharge_date
    plan.uncertainty = "confirmed" if certainty == "confirmed" else "provisional"
    plan.updated_by = user
    plan.save(update_fields=[
        "status",
        "planned_admission_date",
        "planned_discharge_date",
        "estimated_discharge_date",
        "uncertainty",
        "updated_by",
        "updated_at",
    ])

    assignment_values = {
        "planned_start_date": room_start_date,
        "planned_end_date": room_end_date,
        "status": "planned",
        "certainty": certainty,
        "updated_by": user,
    }
    if existing_assignment is None:
        assignment = ResourceAssignment.objects.create(
            treatment_course=locked_course,
            resource_pool=locked_pool,
            created_by=user,
            **assignment_values,
        )
    else:
        for field, value in assignment_values.items():
            setattr(existing_assignment, field, value)
        existing_assignment.save()
        assignment = existing_assignment

    locked_course.admission_date = admission_date
    locked_course.first_treatment_date = treatment_start_date
    locked_course.save(update_fields=["admission_date", "first_treatment_date", "updated_at"])

    if locked_waitlist_entry is not None:
        locked_waitlist_entry.status = "scheduled"
        locked_waitlist_entry.scheduled_at = timezone.now()
        locked_waitlist_entry.inpatient_plan = plan
        locked_waitlist_entry.save(update_fields=[
            "status", "scheduled_at", "inpatient_plan", "updated_at",
        ])

    return {
        "plan": plan,
        "assignment": assignment,
        "capacity": capacity,
        "reservation": reservation,
    }
