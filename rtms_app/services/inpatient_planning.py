import sqlite3
import time
from functools import wraps
from datetime import date, datetime, timedelta

from django.db import DatabaseError, OperationalError, connection, transaction
from django.db.models import Q
from django.utils import timezone

from rtms_app.models import (
    AuditLog,
    InpatientPlan,
    MappingSchedule,
    MappingSession,
    Patient,
    ResourceAssignment,
    ResourcePool,
    RtmSWaitlistEntry,
    RtmSTreatmentReservation,
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


class ProvisionalRollbackError(ValueError):
    """Raised when a provisional schedule is not safe to return to waiting."""


class ScheduleConfirmationBusyError(RuntimeError):
    """Raised when SQLite remains busy after bounded schedule-confirm retries."""


CONFIRM_SQLITE_BUSY_RETRIES = 2
CONFIRM_SQLITE_BUSY_BACKOFF_SECONDS = (0.05, 0.1)
_SQLITE_BUSY_CODES = {getattr(sqlite3, "SQLITE_BUSY", 5), getattr(sqlite3, "SQLITE_LOCKED", 6)}
PROVISIONAL_SCHEDULE_BASELINE_EVENT = "provisional_schedule_baseline"


def _provisional_schedule_baseline(entry, *, for_update=False):
    try:
        logs = AuditLog.objects.filter(
            target_model="RtmSWaitlistEntry",
            target_pk=str(entry.pk),
        )
        if for_update:
            logs = logs.select_for_update()
        logs = logs.order_by("-created_at", "-pk")
        for log in logs:
            if not isinstance(log.meta, dict):
                raise ProvisionalRollbackError(
                    "仮予定の基準情報が破損しているため、待機状態へ戻せません。"
                )
            if log.meta.get("event") == PROVISIONAL_SCHEDULE_BASELINE_EVENT:
                return log
    except ProvisionalRollbackError:
        raise
    except (ValueError, TypeError, DatabaseError) as exc:
        raise ProvisionalRollbackError(
            "仮予定の基準情報を読み取れないため、待機状態へ戻せません。"
        ) from exc
    return None


def _iso_value(value):
    return value.isoformat() if value is not None else None


def _require_baseline_object(value, field_name):
    if not isinstance(value, dict):
        raise ProvisionalRollbackError(
            f"仮予定の基準情報({field_name})が不正なため、待機状態へ戻せません。"
        )
    return value


def _require_baseline_keys(value, fields, field_name):
    if not set(fields).issubset(value):
        raise ProvisionalRollbackError(
            f"仮予定の基準情報({field_name})に必須項目がないため、待機状態へ戻せません。"
        )


def _require_baseline_integer(value, field_name, *, nullable=False):
    if nullable and value is None:
        return
    if type(value) is not int:
        raise ProvisionalRollbackError(
            f"仮予定の基準情報({field_name})が不正なため、待機状態へ戻せません。"
        )


def _require_baseline_boolean(value, field_name):
    if type(value) is not bool:
        raise ProvisionalRollbackError(
            f"仮予定の基準情報({field_name})が不正なため、待機状態へ戻せません。"
        )


def _require_baseline_date(value, field_name, *, nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value, str):
        raise ProvisionalRollbackError(
            f"仮予定の基準情報({field_name})が不正なため、待機状態へ戻せません。"
        )
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ProvisionalRollbackError(
            f"仮予定の基準情報({field_name})が不正なため、待機状態へ戻せません。"
        ) from exc
    if parsed.isoformat() != value:
        raise ProvisionalRollbackError(
            f"仮予定の基準情報({field_name})が不正なため、待機状態へ戻せません。"
        )


def _require_baseline_datetime(value, field_name, *, nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value, str):
        raise ProvisionalRollbackError(
            f"仮予定の基準情報({field_name})が不正なため、待機状態へ戻せません。"
        )
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ProvisionalRollbackError(
            f"仮予定の基準情報({field_name})が不正なため、待機状態へ戻せません。"
        ) from exc
    if parsed.isoformat() != value:
        raise ProvisionalRollbackError(
            f"仮予定の基準情報({field_name})が不正なため、待機状態へ戻せません。"
        )


def _validate_provisional_schedule_rollback(course, entry, *, lock_baseline=False):
    if entry.status != "provisional":
        raise ProvisionalRollbackError(
            "この患者は現在「日程調整中」ではないため、待機状態へ戻せません。"
        )
    if entry.treatment_course_id != course.pk:
        raise ProvisionalRollbackError("待機リストと対象Courseが一致しません。")
    plan = InpatientPlan.objects.filter(treatment_course=course).first()
    if plan is None:
        raise ProvisionalRollbackError("対象Courseに紐づく入院Planを確認できません。")
    baseline_log = _provisional_schedule_baseline(entry, for_update=lock_baseline)
    if baseline_log is None:
        raise ProvisionalRollbackError(
            "仮予定前の状態を確認できないため、自動では待機状態へ戻せません。"
        )
    baseline = _require_baseline_object(baseline_log.meta, "root")
    _require_baseline_keys(
        baseline,
        ("event", "course_id", "course_number", "waitlist_entry_id", "entry_before",
         "course_before", "plan_before", "provisional_values"),
        "root",
    )
    if baseline.get("event") != PROVISIONAL_SCHEDULE_BASELINE_EVENT:
        raise ProvisionalRollbackError("仮予定の基準情報を確認できません。")
    _require_baseline_integer(baseline.get("course_id"), "course_id")
    _require_baseline_integer(baseline.get("course_number"), "course_number")
    _require_baseline_integer(baseline.get("waitlist_entry_id"), "waitlist_entry_id")
    if (
        baseline["course_id"] != course.pk
        or baseline["course_number"] != course.course_number
        or baseline["waitlist_entry_id"] != entry.pk
        or baseline_log.target_model != "RtmSWaitlistEntry"
        or baseline_log.target_pk != str(entry.pk)
        or baseline_log.patient_id != course.patient_id
    ):
        raise ProvisionalRollbackError("仮予定の基準情報と対象Courseまたは待機リストが一致しません。")

    entry_before = _require_baseline_object(baseline.get("entry_before"), "entry_before")
    course_before = _require_baseline_object(baseline.get("course_before"), "course_before")
    plan_before = baseline.get("plan_before")
    provisional_values = _require_baseline_object(
        baseline.get("provisional_values"), "provisional_values",
    )
    provisional_course = _require_baseline_object(
        provisional_values.get("course"), "provisional_values.course",
    )
    provisional_plan = _require_baseline_object(
        provisional_values.get("plan"), "provisional_values.plan",
    )
    provisional_entry = _require_baseline_object(
        provisional_values.get("entry"), "provisional_values.entry",
    )
    _require_baseline_keys(
        entry_before, ("status", "inpatient_plan_id", "scheduled_at"), "entry_before",
    )
    _require_baseline_keys(
        course_before,
        ("admission_date", "first_treatment_date", "mapping_date", "is_all_case_survey",
         "private_room_planned", "planned_treatment_sessions"),
        "course_before",
    )
    _require_baseline_keys(
        provisional_values, ("course", "plan", "entry"), "provisional_values",
    )
    _require_baseline_keys(
        provisional_course,
        ("admission_date", "first_treatment_date", "mapping_date", "is_all_case_survey",
         "private_room_planned", "planned_treatment_sessions"),
        "provisional_values.course",
    )
    _require_baseline_keys(
        provisional_plan,
        ("id", "status", "planned_admission_date", "actual_admission_at",
         "actual_discharge_at", "uncertainty"),
        "provisional_values.plan",
    )
    _require_baseline_keys(
        provisional_entry, ("status", "inpatient_plan_id", "scheduled_at"),
        "provisional_values.entry",
    )

    if (
        not isinstance(entry_before["status"], str)
        or entry_before["status"] not in {"requested", "waiting"}
    ):
        raise ProvisionalRollbackError("仮予定前の待機状態を確認できません。")
    _require_baseline_integer(
        entry_before.get("inpatient_plan_id"), "entry_before.inpatient_plan_id", nullable=True,
    )
    old_scheduled_at = entry_before.get("scheduled_at")
    _require_baseline_datetime(old_scheduled_at, "entry_before.scheduled_at", nullable=True)

    course_before_fields = (
        "admission_date", "first_treatment_date", "mapping_date",
    )
    course_flag_fields = ("is_all_case_survey", "private_room_planned")
    for field in course_before_fields:
        _require_baseline_date(course_before.get(field), f"course_before.{field}", nullable=True)
    for field in course_flag_fields:
        _require_baseline_boolean(course_before.get(field), f"course_before.{field}")
    _require_baseline_integer(
        course_before.get("planned_treatment_sessions"),
        "course_before.planned_treatment_sessions",
    )
    if any(course_before.get(field) is not None for field in (
        "admission_date", "first_treatment_date", "mapping_date",
    )):
        raise ProvisionalRollbackError(
            "既存の治療スケジュール情報があるため、自動では待機状態へ戻せません。"
        )
    if plan_before is not None:
        plan_before = _require_baseline_object(plan_before, "plan_before")
        _require_baseline_keys(
            plan_before,
            ("id", "status", "planned_admission_date", "actual_admission_at",
             "actual_discharge_at", "uncertainty"),
            "plan_before",
        )
        _require_baseline_integer(plan_before.get("id"), "plan_before.id")
        if plan_before.get("status") != "draft":
            raise ProvisionalRollbackError("既存の入院予定情報があるため、自動では戻せません。")
        _require_baseline_date(
            plan_before.get("planned_admission_date"),
            "plan_before.planned_admission_date", nullable=True,
        )
        for field in ("actual_admission_at", "actual_discharge_at"):
            _require_baseline_datetime(
                plan_before.get(field), f"plan_before.{field}", nullable=True,
            )
        if (
            plan_before.get("planned_admission_date") is not None
            or plan_before.get("actual_admission_at") is not None
            or plan_before.get("actual_discharge_at") is not None
        ):
            raise ProvisionalRollbackError(
                "既存の入院予定情報があるため、自動では待機状態へ戻せません。"
            )
        if not isinstance(plan_before.get("uncertainty"), str):
            raise ProvisionalRollbackError("仮予定前のPlan状態を確認できません。")
    old_entry_plan_id = entry_before["inpatient_plan_id"]
    old_plan = None
    if old_entry_plan_id is not None:
        if plan_before is None or plan_before.get("id") != old_entry_plan_id:
            raise ProvisionalRollbackError("仮予定前のPlan関連を確認できません。")
        old_plan = InpatientPlan.objects.filter(pk=old_entry_plan_id).first()
        if old_plan is None or old_plan.treatment_course_id != course.pk:
            raise ProvisionalRollbackError("仮予定前のPlanが別Courseに属するため戻せません。")
    if plan_before is not None:
        if old_plan is None:
            old_plan = InpatientPlan.objects.filter(pk=plan_before["id"]).first()
        if old_plan is None or old_plan.treatment_course_id != course.pk:
            raise ProvisionalRollbackError("仮予定前のPlanが対象Courseに属することを確認できません。")

    provisional_course_fields = (
        "admission_date", "first_treatment_date", "mapping_date",
    )
    for field in provisional_course_fields:
        _require_baseline_date(
            provisional_course.get(field), f"provisional_values.course.{field}", nullable=True,
        )
    for field in course_flag_fields:
        _require_baseline_boolean(
            provisional_course.get(field), f"provisional_values.course.{field}",
        )
    _require_baseline_integer(
        provisional_course.get("planned_treatment_sessions"),
        "provisional_values.course.planned_treatment_sessions",
    )
    course_values = {
        "admission_date": _iso_value(course.admission_date),
        "first_treatment_date": _iso_value(course.first_treatment_date),
        "mapping_date": _iso_value(course.mapping_date),
        "is_all_case_survey": course.is_all_case_survey,
        "private_room_planned": course.private_room_planned,
        "planned_treatment_sessions": course.planned_treatment_sessions,
    }
    if course_values != provisional_course:
        raise ProvisionalRollbackError(
            "Course情報が仮予定保存後に変更されているため、待機状態へ戻せません。"
        )

    _require_baseline_integer(provisional_plan["id"], "provisional_values.plan.id")
    if provisional_plan["status"] != "provisional":
        raise ProvisionalRollbackError("仮予定保存後のPlan状態を確認できません。")
    _require_baseline_date(
        provisional_plan["planned_admission_date"],
        "provisional_values.plan.planned_admission_date",
    )
    for field in ("actual_admission_at", "actual_discharge_at"):
        _require_baseline_datetime(
            provisional_plan[field], f"provisional_values.plan.{field}", nullable=True,
        )
    if not isinstance(provisional_plan["uncertainty"], str):
        raise ProvisionalRollbackError("仮予定保存後のPlan状態を確認できません。")
    if plan_before is not None and plan_before["id"] != provisional_plan["id"]:
        raise ProvisionalRollbackError("仮予定のPlan IDが基準情報と一致しません。")
    plan_values = {
        "id": plan.pk,
        "status": plan.status,
        "planned_admission_date": _iso_value(plan.planned_admission_date),
        "actual_admission_at": _iso_value(plan.actual_admission_at),
        "actual_discharge_at": _iso_value(plan.actual_discharge_at),
        "uncertainty": plan.uncertainty,
    }
    if plan_values != provisional_plan:
        raise ProvisionalRollbackError(
            "入院Planが仮予定保存後に変更されているため、待機状態へ戻せません。"
        )

    _require_baseline_datetime(
        provisional_entry["scheduled_at"],
        "provisional_values.entry.scheduled_at", nullable=True,
    )
    if provisional_entry["status"] != "provisional":
        raise ProvisionalRollbackError("仮予定保存後に待機リストが変更されています。")
    _require_baseline_integer(
        provisional_entry["inpatient_plan_id"],
        "provisional_values.entry.inpatient_plan_id", nullable=True,
    )
    if (
        provisional_entry["scheduled_at"] != _iso_value(entry.scheduled_at)
        or entry.inpatient_plan_id != provisional_entry["inpatient_plan_id"]
    ):
        raise ProvisionalRollbackError(
            "待機リストと入院Planの関連が仮予定保存後に変更されています。"
        )
    if entry.inpatient_plan_id != plan.pk or plan.treatment_course_id != course.pk:
        raise ProvisionalRollbackError("仮予定のPlanが対象Courseに属することを確認できません。")

    if TreatmentSession.objects.filter(
        Q(treatment_course=course)
        | Q(
            treatment_course__isnull=True,
            patient_id=course.patient_id,
            course_number=course.course_number,
        )
    ).exists():
        raise ProvisionalRollbackError(
            "すでに治療予定・治療記録が作成されているため、この画面から待機状態へ戻せません。"
        )
    if ResourceAssignment.objects.filter(treatment_course=course).exists():
        raise ProvisionalRollbackError(
            "入院場所の割当があるため、この画面から待機状態へ戻せません。"
        )
    if MappingSession.objects.filter(
        Q(treatment_course=course)
        | Q(
            treatment_course__isnull=True,
            patient_id=course.patient_id,
            course_number=course.course_number,
        )
    ).exists():
        raise ProvisionalRollbackError(
            "位置決め実績があるため、この画面から待機状態へ戻せません。"
        )
    if MappingSchedule.objects.filter(
        Q(treatment_course=course)
        | Q(
            treatment_course__isnull=True,
            patient_id=course.patient_id,
            course_number=course.course_number,
        )
    ).exists():
        raise ProvisionalRollbackError(
            "位置決め予定が残っているため、この画面から待機状態へ戻せません。"
        )
    reservations = list(
        RtmSTreatmentReservation.objects.filter(treatment_course=course)
        .order_by("sequence_number")
    )
    if any(reservation.status == "confirmed" for reservation in reservations):
        raise ProvisionalRollbackError(
            "確定済みの治療予約があるため、この画面から待機状態へ戻せません。"
        )
    if (
        len(reservations) != 30
        or [item.sequence_number for item in reservations] != list(range(1, 31))
        or any(item.status != "provisional" for item in reservations)
    ):
        raise ProvisionalRollbackError(
            "仮予定の治療予約を安全に確認できないため、待機状態へ戻せません。"
        )
    if plan.actual_admission_at is not None or plan.status == "admitted":
        raise ProvisionalRollbackError(
            "実入院済みのCourseは待機状態へ戻せません。"
        )
    return baseline_log, baseline, plan, reservations


def can_rollback_provisional_schedule(*, treatment_course, waitlist_entry):
    """Return whether this Course has a verifiably clean provisional schedule."""
    try:
        _validate_provisional_schedule_rollback(treatment_course, waitlist_entry)
    except (ProvisionalRollbackError, InpatientPlan.DoesNotExist):
        return False
    return True


@transaction.atomic
def rollback_provisional_schedule(
    *, treatment_course, waitlist_entry, user=None, ip=None, user_agent="",
):
    """Atomically return one clean Course and its waitlist entry to waiting."""
    course = TreatmentCourse.objects.select_for_update().get(pk=treatment_course.pk)
    entry = RtmSWaitlistEntry.objects.select_for_update().get(pk=waitlist_entry.pk)
    if entry.treatment_course_id != course.pk:
        raise ProvisionalRollbackError("待機リストと対象Courseが一致しません。")
    plan = InpatientPlan.objects.select_for_update().filter(
        treatment_course=course,
    ).first()
    if plan is None:
        raise ProvisionalRollbackError("対象Courseに紐づく入院Planを確認できません。")
    reservations = list(
        RtmSTreatmentReservation.objects.select_for_update()
        .filter(treatment_course=course)
        .order_by("sequence_number")
    )
    baseline_log, baseline, plan, reservations = _validate_provisional_schedule_rollback(
        course, entry, lock_baseline=True,
    )
    course_before = baseline["course_before"]

    course.admission_date = date.fromisoformat(course_before["admission_date"]) if course_before["admission_date"] else None
    course.first_treatment_date = date.fromisoformat(course_before["first_treatment_date"]) if course_before["first_treatment_date"] else None
    course.mapping_date = date.fromisoformat(course_before["mapping_date"]) if course_before["mapping_date"] else None
    course.is_all_case_survey = course_before["is_all_case_survey"]
    course.private_room_planned = course_before["private_room_planned"]
    course.planned_treatment_sessions = course_before["planned_treatment_sessions"]
    course.save(update_fields=[
        "admission_date", "first_treatment_date", "mapping_date",
        "is_all_case_survey", "private_room_planned", "planned_treatment_sessions",
        "updated_at",
    ])

    plan.status = "draft"
    plan.planned_admission_date = None
    plan.updated_by = user
    plan.save(update_fields=["status", "planned_admission_date", "updated_by", "updated_at"])

    RtmSTreatmentReservation.objects.filter(
        treatment_course=course,
        status="provisional",
    ).update(status="cancelled", updated_at=timezone.now())

    entry.status = "waiting"
    entry.inpatient_plan_id = (baseline.get("entry_before") or {}).get("inpatient_plan_id")
    entry.save(update_fields=["status", "inpatient_plan", "updated_at"])

    AuditLog.objects.create(
        user=user,
        patient=course.patient,
        target_model="RtmSWaitlistEntry",
        target_pk=str(entry.pk),
        action="UPDATE",
        summary="rTMS provisional schedule rollback to waiting",
        meta={
            "event": "provisional_schedule_rollback",
            "reason": "user_requested_waiting_rollback",
            "course_id": course.pk,
            "course_number": course.course_number,
            "waitlist_entry_id": entry.pk,
            "inpatient_plan_id": plan.pk,
            "previous_waitlist_status": "provisional",
            "new_waitlist_status": "waiting",
            "previous_plan_status": "provisional",
            "new_plan_status": "draft",
            "cancelled_reservation_count": len(reservations),
            "baseline_audit_id": baseline_log.pk,
        },
        ip=ip,
        user_agent=user_agent,
    )
    return {"course": course, "waitlist_entry": entry, "plan": plan}


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
    locked_entry = None
    baseline_log = None
    if waitlist_entry is not None:
        locked_entry = RtmSWaitlistEntry.objects.select_for_update().get(pk=waitlist_entry.pk)
        if locked_entry.treatment_course_id != locked_course.pk:
            raise InvalidWaitlistEntryError("待機リストと日程設定対象のCourseが一致しません。")
        if locked_entry.status not in {"requested", "waiting", "provisional"}:
            raise InvalidWaitlistEntryError("この状態の待機リストは仮予定にできません。")
        if locked_entry.status in {"requested", "waiting"}:
            previous_plan = InpatientPlan.objects.select_for_update().filter(
                treatment_course=locked_course,
            ).first()
            baseline_log = AuditLog.objects.create(
                user=user,
                patient=locked_patient,
                target_model="RtmSWaitlistEntry",
                target_pk=str(locked_entry.pk),
                action="UPDATE",
                summary="rTMS provisional schedule baseline",
                meta={
                    "event": PROVISIONAL_SCHEDULE_BASELINE_EVENT,
                    "course_id": locked_course.pk,
                    "course_number": locked_course.course_number,
                    "waitlist_entry_id": locked_entry.pk,
                    "entry_before": {
                        "status": locked_entry.status,
                        "inpatient_plan_id": locked_entry.inpatient_plan_id,
                        "scheduled_at": _iso_value(locked_entry.scheduled_at),
                    },
                    "course_before": {
                        "admission_date": _iso_value(locked_course.admission_date),
                        "first_treatment_date": _iso_value(locked_course.first_treatment_date),
                        "mapping_date": _iso_value(locked_course.mapping_date),
                        "is_all_case_survey": locked_course.is_all_case_survey,
                        "private_room_planned": locked_course.private_room_planned,
                        "planned_treatment_sessions": locked_course.planned_treatment_sessions,
                    },
                    "plan_before": None if previous_plan is None else {
                        "id": previous_plan.pk,
                        "status": previous_plan.status,
                        "planned_admission_date": _iso_value(previous_plan.planned_admission_date),
                        "actual_admission_at": _iso_value(previous_plan.actual_admission_at),
                        "actual_discharge_at": _iso_value(previous_plan.actual_discharge_at),
                        "uncertainty": previous_plan.uncertainty,
                    },
                },
            )
        else:
            baseline_log = _provisional_schedule_baseline(locked_entry)
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
        waitlist_entry=locked_entry,
        user=user,
    )
    plan = get_or_create_inpatient_plan(treatment_course=locked_course, user=user)
    plan.planned_admission_date = admission_date
    plan.updated_by = user
    plan.save(update_fields=["planned_admission_date", "updated_by", "updated_at"])
    if baseline_log is not None:
        locked_entry.refresh_from_db()
        plan.refresh_from_db()
        baseline_log.meta["provisional_values"] = {
            "course": {
                "admission_date": _iso_value(locked_course.admission_date),
                "first_treatment_date": _iso_value(locked_course.first_treatment_date),
                "mapping_date": _iso_value(locked_course.mapping_date),
                "is_all_case_survey": locked_course.is_all_case_survey,
                "private_room_planned": locked_course.private_room_planned,
                "planned_treatment_sessions": locked_course.planned_treatment_sessions,
            },
            "plan": {
                "id": plan.pk,
                "status": plan.status,
                "planned_admission_date": _iso_value(plan.planned_admission_date),
                "actual_admission_at": _iso_value(plan.actual_admission_at),
                "actual_discharge_at": _iso_value(plan.actual_discharge_at),
                "uncertainty": plan.uncertainty,
            },
            "entry": {
                "status": locked_entry.status,
                "inpatient_plan_id": locked_entry.inpatient_plan_id,
                "scheduled_at": _iso_value(locked_entry.scheduled_at),
            },
        }
        baseline_log.save(update_fields=["meta"])
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
