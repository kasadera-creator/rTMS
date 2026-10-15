import sqlite3
import time
from datetime import date, timedelta

from django.db import OperationalError, connection, transaction
from django.db.models import Count, Exists, F, OuterRef, Q
from django.utils import timezone

from rtms_app.models import (
    InpatientPlan,
    RtmSTreatmentCapacity,
    RtmSTreatmentReservation,
    RtmSWaitlistEntry,
    TreatmentCourse,
    TreatmentSession,
)
from rtms_app.services.rtms_schedule import (
    generate_course_treatment_dates,
    generate_treatment_dates,
    get_japanese_holidays,
    is_closed,
)


CAPACITY_TYPES = ("rTMS_private", "general_ward")
ACTIVE_RESERVATION_STATUSES = ("provisional", "confirmed")
RESERVATION_BUSY_RETRIES = 2
RESERVATION_BUSY_BACKOFF_SECONDS = (0.05, 0.1)
_SQLITE_BUSY_CODES = {
    getattr(sqlite3, "SQLITE_BUSY", 5),
    getattr(sqlite3, "SQLITE_LOCKED", 6),
}


class TreatmentCapacityConfigurationError(ValueError):
    """Raised when a treatment date lacks exactly one capacity setting per type."""


class TreatmentReservationCapacityConflict(ValueError):
    """Raised when one or more dates cannot fit in the configured treatment slots."""

    def __init__(self, conflicts):
        self.conflicts = conflicts
        dates = ", ".join(item["date"].isoformat() for item in conflicts)
        super().__init__(f"以下の治療日にrTMS治療枠の空きがありません: {dates}")


class InvalidTreatmentReservationError(ValueError):
    """Raised when a Course cannot be scheduled using the supplied dates/state."""


class TreatmentReservationBusyError(RuntimeError):
    """Raised when SQLite remains busy after bounded reservation retries."""


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


def _run_with_sqlite_busy_retry(operation):
    can_retry = not connection.in_atomic_block
    for attempt in range(RESERVATION_BUSY_RETRIES + 1):
        try:
            with transaction.atomic():
                return operation()
        except OperationalError as exc:
            if not _is_sqlite_busy_error(exc):
                raise
            if not can_retry or attempt == RESERVATION_BUSY_RETRIES:
                raise TreatmentReservationBusyError(
                    "同時処理のためrTMS治療予定を保存できませんでした。もう一度お試しください。"
                ) from exc
            time.sleep(RESERVATION_BUSY_BACKOFF_SECONDS[attempt])


def _reserve_sqlite_writer(start_date, end_date):
    settings = RtmSTreatmentCapacity.objects.filter(
        is_active=True,
        valid_from__lte=end_date,
    ).filter(
        Q(valid_to__isnull=True) | Q(valid_to__gte=start_date),
    )
    if connection.vendor == "sqlite":
        settings.update(capacity=F("capacity"))
    else:
        list(settings.select_for_update().values_list("pk", flat=True))


def _settings_for_dates(dates, *, allow_unconfigured=False):
    start_date, end_date = dates[0], dates[-1]
    settings = list(RtmSTreatmentCapacity.objects.filter(
        is_active=True,
        valid_from__lte=end_date,
    ).filter(
        Q(valid_to__isnull=True) | Q(valid_to__gte=start_date),
    ).order_by("capacity_type", "valid_from", "pk"))
    settings_by_date = {}
    for treatment_date in dates:
        settings_by_type = {}
        for capacity_type in CAPACITY_TYPES:
            matching = [
                setting for setting in settings
                if setting.capacity_type == capacity_type
                and setting.valid_from <= treatment_date
                and (setting.valid_to is None or treatment_date <= setting.valid_to)
            ]
            if not matching and allow_unconfigured:
                settings_by_type[capacity_type] = None
                continue
            if len(matching) != 1:
                raise TreatmentCapacityConfigurationError(
                    f"{treatment_date.isoformat()}の{capacity_type}治療枠設定は"
                    f"1件必要です（現在{len(matching)}件）。"
                )
            settings_by_type[capacity_type] = matching[0].capacity
        settings_by_date[treatment_date] = settings_by_type
    return settings_by_date


def _occupancy_for_dates(dates, *, exclude_course_id=None):
    reservations = RtmSTreatmentReservation.objects.filter(
        treatment_date__in=dates,
        status__in=ACTIVE_RESERVATION_STATUSES,
    )
    if exclude_course_id is not None:
        reservations = reservations.exclude(treatment_course_id=exclude_course_id)
    return {
        (row["treatment_date"], row["capacity_type"]): row["occupied"]
        for row in reservations.values("treatment_date", "capacity_type").annotate(
            occupied=Count("pk"),
        )
    }


def treatment_capacity_snapshot(treatment_date, *, exclude_course_id=None):
    return treatment_capacity_snapshots(
        [treatment_date], exclude_course_id=exclude_course_id,
    )[0]


def treatment_capacity_snapshots(
    treatment_dates, *, exclude_course_id=None, allow_unconfigured=False,
):
    """Return service-owned occupancy snapshots for multiple dates in one query pair."""
    treatment_dates = list(treatment_dates)
    if not treatment_dates:
        return []
    settings_by_date = _settings_for_dates(
        treatment_dates,
        allow_unconfigured=allow_unconfigured,
    )
    occupied = _occupancy_for_dates(
        treatment_dates, exclude_course_id=exclude_course_id,
    )
    snapshots = []
    for treatment_date in treatment_dates:
        by_type = {}
        for capacity_type in CAPACITY_TYPES:
            category_occupied = occupied.get((treatment_date, capacity_type), 0)
            capacity = settings_by_date[treatment_date][capacity_type]
            by_type[capacity_type] = {
                "capacity": capacity,
                "occupied": category_occupied,
                "remaining": (
                    max(capacity - category_occupied, 0)
                    if capacity is not None else None
                ),
            }
        total_capacity = (
            sum(item["capacity"] for item in by_type.values())
            if all(item["capacity"] is not None for item in by_type.values())
            else None
        )
        total_occupied = sum(item["occupied"] for item in by_type.values())
        snapshots.append({
            "date": treatment_date,
            **by_type,
            "total_capacity": total_capacity,
            "total_occupied": total_occupied,
            "total_remaining": (
                max(total_capacity - total_occupied, 0)
                if total_capacity is not None else None
            ),
        })
    return snapshots


def preview_rtms_treatment_reservation(
    *, first_treatment_date: date, exclude_course_id=None,
):
    """Preview the canonical 30 treatment dates and capacity without writing rows."""
    holiday_end = first_treatment_date + timedelta(days=365)
    holidays = get_japanese_holidays(first_treatment_date, holiday_end)
    dates = generate_treatment_dates(first_treatment_date, total=30, holidays=holidays)
    if len(dates) != 30 or dates[0] != first_treatment_date:
        raise InvalidTreatmentReservationError(
            "初回治療日は平日かつ休診日以外の日付を指定してください。"
        )
    settings = _settings_for_dates(dates)
    occupancy = _occupancy_for_dates(dates, exclude_course_id=exclude_course_id)
    try:
        capacity_types, snapshots = _allocate_capacity_types(dates, settings, occupancy)
    except TreatmentReservationCapacityConflict as exc:
        snapshots = exc.conflicts
        capacity_types = []
    return {
        "treatment_dates": dates,
        "treatment_end_date": dates[-1],
        "capacity_types": capacity_types,
        "capacity_snapshots": snapshots,
        "conflicts": [
            snapshot for snapshot in snapshots if snapshot["total_remaining"] <= 0
        ],
    }


def _allocate_capacity_types(dates, settings, occupancy):
    assignments = []
    conflicts = []
    snapshots = []
    for treatment_date in dates:
        day_settings = settings[treatment_date]
        day_occupancy = {
            capacity_type: occupancy.get((treatment_date, capacity_type), 0)
            for capacity_type in CAPACITY_TYPES
        }
        total_capacity = sum(day_settings.values())
        total_occupied = sum(day_occupancy.values())
        snapshot = {
            "date": treatment_date,
            **{
                capacity_type: {
                    "capacity": day_settings[capacity_type],
                    "occupied": day_occupancy[capacity_type],
                    "remaining": max(
                        day_settings[capacity_type] - day_occupancy[capacity_type], 0,
                    ),
                }
                for capacity_type in CAPACITY_TYPES
            },
            "total_capacity": total_capacity,
            "total_occupied": total_occupied,
            "total_remaining": max(total_capacity - total_occupied, 0),
        }
        snapshots.append(snapshot)
        available_type = next(
            (
                capacity_type for capacity_type in CAPACITY_TYPES
                if day_occupancy[capacity_type] < day_settings[capacity_type]
            ),
            None,
        )
        if total_occupied >= total_capacity or available_type is None:
            conflicts.append(snapshot)
        else:
            assignments.append(available_type)
            occupancy[(treatment_date, available_type)] = day_occupancy[available_type] + 1
    if conflicts:
        raise TreatmentReservationCapacityConflict(conflicts)
    return assignments, snapshots


def _estimate_available_courses_for_dates(dates, settings, occupancy):
    return min(
        max(
            sum(settings[treatment_date].values())
            - occupancy.get(treatment_date, 0),
            0,
        )
        for treatment_date in dates
    )


def _total_occupancy_for_dates(dates):
    date_set = set(dates)
    if not date_set:
        return {}
    sessions_in_range = list(
        TreatmentSession.objects.filter(
            session_date__in=date_set,
            status__in=("planned", "done", "skipped"),
        ).values("pk", "treatment_course_id", "patient_id", "course_number", "session_date", "status")
    )
    session_course_ids = {
        session["treatment_course_id"]
        for session in sessions_in_range
        if session["treatment_course_id"] is not None
    }
    legacy_patient_ids = {
        session["patient_id"]
        for session in sessions_in_range
        if session["treatment_course_id"] is None
    }
    legacy_course_map = {
        (course["patient_id"], course["course_number"]): course["pk"]
        for course in TreatmentCourse.objects.filter(
            patient_id__in=legacy_patient_ids,
        ).values("pk", "patient_id", "course_number")
    }
    session_course_ids.update(legacy_course_map.values())
    planned_course_rows = list(
        TreatmentCourse.objects.filter(
            first_treatment_date__gte=min(date_set) - timedelta(days=365),
            first_treatment_date__lte=max(date_set),
        ).values("pk", "patient_id", "course_number", "first_treatment_date")
    )
    session_course_ids.update(course["pk"] for course in planned_course_rows)
    reservation_query = RtmSTreatmentReservation.objects.filter(
        status__in=ACTIVE_RESERVATION_STATUSES,
    ).filter(
        Q(treatment_date__in=date_set)
        | Q(treatment_course_id__in=session_course_ids)
    )
    reservations = list(reservation_query.values(
        "treatment_course_id", "sequence_number", "treatment_date",
    ))
    active_reservation_keys = {
        (item["treatment_course_id"], item["sequence_number"])
        for item in reservations
    }
    sessions_by_course = {}
    if session_course_ids or legacy_patient_ids:
        course_sessions = TreatmentSession.objects.filter(
            Q(treatment_course_id__in=session_course_ids)
            | Q(treatment_course__isnull=True, patient_id__in=legacy_patient_ids)
        ).order_by("session_date", "pk").values(
            "pk", "treatment_course_id", "session_date", "status",
            "patient_id", "course_number",
        )
        for session in course_sessions:
            course_id = session["treatment_course_id"] or legacy_course_map.get(
                (session["patient_id"], session["course_number"]),
            )
            if course_id is not None:
                sessions_by_course.setdefault(course_id, []).append(session)

    skipped_reservation_keys = set()
    materialized_sequence_keys = set(active_reservation_keys)
    for course_id, course_sessions in sessions_by_course.items():
        for sequence_number, session in enumerate(course_sessions, start=1):
            key = (course_id, sequence_number)
            materialized_sequence_keys.add(key)
            if session["status"] == "skipped":
                skipped_reservation_keys.add(key)

    occupancy = {treatment_date: 0 for treatment_date in date_set}
    for reservation in reservations:
        key = (reservation["treatment_course_id"], reservation["sequence_number"])
        if reservation["treatment_date"] in date_set and key not in skipped_reservation_keys:
            occupancy[reservation["treatment_date"]] += 1

    session_sequence_by_id = {}
    for course_id, course_sessions in sessions_by_course.items():
        for sequence_number, session in enumerate(course_sessions, start=1):
            session_sequence_by_id[session["pk"]] = sequence_number
    for session in sessions_in_range:
        if session["status"] not in {"planned", "done"}:
            continue
        course_id = session["treatment_course_id"] or legacy_course_map.get(
            (session["patient_id"], session["course_number"]),
        )
        sequence_number = session_sequence_by_id.get(session["pk"])
        if course_id is not None and sequence_number is not None:
            if (course_id, sequence_number) in active_reservation_keys:
                continue
        occupancy[session["session_date"]] += 1

    materialized_course_dates = {
        (item["treatment_course_id"], item["treatment_date"])
        for item in reservations
        if item["treatment_course_id"] is not None
        and item["treatment_date"] in date_set
    }
    for session in sessions_in_range:
        course_id = session["treatment_course_id"] or legacy_course_map.get(
            (session["patient_id"], session["course_number"]),
        )
        if course_id is not None:
            materialized_course_dates.add((course_id, session["session_date"]))

    if planned_course_rows:
        course_start_dates = [row["first_treatment_date"] for row in planned_course_rows]
        course_holidays = get_japanese_holidays(
            min(course_start_dates),
            max(course_start_dates) + timedelta(days=365),
        )
        for course in planned_course_rows:
            for sequence_number, treatment_date in enumerate(generate_course_treatment_dates(
                course["first_treatment_date"],
                holidays=course_holidays,
            ), start=1):
                if (
                    treatment_date in date_set
                    and (course["pk"], sequence_number) not in materialized_sequence_keys
                    and (course["pk"], treatment_date) not in materialized_course_dates
                ):
                    occupancy[treatment_date] += 1
    return occupancy


def estimate_available_new_courses_by_start_dates(start_dates):
    """Estimate independent new-course capacity for a batch of week starts."""
    start_dates = list(dict.fromkeys(start_dates))
    if not start_dates:
        return {}

    holiday_end = max(start_dates) + timedelta(days=365)
    holidays = get_japanese_holidays(min(start_dates), holiday_end)
    treatment_dates_by_start = {}
    for week_start in start_dates:
        first_open_date = next(
            (
                week_start + timedelta(days=offset)
                for offset in range(7)
                if not is_closed(week_start + timedelta(days=offset), holidays)
            ),
            None,
        )
        if first_open_date is None:
            treatment_dates_by_start[week_start] = []
            continue
        treatment_dates = generate_treatment_dates(
            first_open_date,
            total=30,
            holidays=holidays,
        )
        treatment_dates_by_start[week_start] = (
            treatment_dates if len(treatment_dates) == 30 else []
        )

    all_treatment_dates = sorted({
        treatment_date
        for treatment_dates in treatment_dates_by_start.values()
        for treatment_date in treatment_dates
    })
    if not all_treatment_dates:
        return {week_start: 0 for week_start in start_dates}

    settings = _settings_for_dates(
        all_treatment_dates,
        allow_unconfigured=True,
    )
    occupancy = _total_occupancy_for_dates(all_treatment_dates)
    estimates = {}
    for week_start, treatment_dates in treatment_dates_by_start.items():
        if not treatment_dates:
            estimates[week_start] = 0
        elif any(
            any(capacity is None for capacity in settings[treatment_date].values())
            for treatment_date in treatment_dates
        ):
            estimates[week_start] = None
        else:
            estimates[week_start] = _estimate_available_courses_for_dates(
                treatment_dates,
                settings,
                occupancy,
            )
    return estimates


def estimate_available_new_courses(start_date):
    """Estimate how many 30-treatment courses can start in the given week."""
    return estimate_available_new_courses_by_start_dates([start_date])[start_date]


def _validate_waitlist_entry(entry, course):
    if entry is None:
        return None
    locked_entry = RtmSWaitlistEntry.objects.select_for_update().get(pk=entry.pk)
    if locked_entry.treatment_course_id != course.pk:
        raise InvalidTreatmentReservationError("待機リストと治療予約のCourseが一致しません。")
    if locked_entry.status not in {"requested", "waiting", "provisional"}:
        raise InvalidTreatmentReservationError("この状態の待機リストは仮予定にできません。")
    return locked_entry


def _create_or_replace_locked(
    *, treatment_course, admission_date, first_treatment_date,
    treatment_dates=None, cancelled_treatment_dates=None, reservation_status=None,
    waitlist_entry=None, user=None, create_plan=True, update_course=True,
):
    course = TreatmentCourse.objects.select_for_update().get(pk=treatment_course.pk)
    locked_entry = _validate_waitlist_entry(waitlist_entry, course)
    if admission_date > first_treatment_date:
        raise InvalidTreatmentReservationError("初回治療日は入院日以降にしてください。")

    if treatment_dates is None:
        holiday_end = first_treatment_date + timedelta(days=365)
        holidays = get_japanese_holidays(first_treatment_date, holiday_end)
        dates = generate_treatment_dates(first_treatment_date, total=30, holidays=holidays)
    else:
        dates = list(treatment_dates)
    if (
        len(dates) != 30
        or dates[0] != first_treatment_date
        or dates != sorted(set(dates))
    ):
        raise InvalidTreatmentReservationError("初回治療日は平日かつ休診日以外の日付を指定してください。")
    cancelled_dates = set(cancelled_treatment_dates or ())
    if not cancelled_dates.issubset(dates):
        raise InvalidTreatmentReservationError("キャンセル対象日は治療予定日に含まれていません。")

    existing = list(RtmSTreatmentReservation.objects.select_for_update().filter(
        treatment_course=course,
    ).order_by("sequence_number"))
    active_statuses = {
        reservation.status for reservation in existing
        if reservation.status in ACTIVE_RESERVATION_STATUSES
    }
    if reservation_status is None:
        if len(active_statuses) > 1:
            raise InvalidTreatmentReservationError("Course内の治療予約状態が一致していません。")
        reservation_status = next(iter(active_statuses), "provisional")
    if reservation_status not in ACTIVE_RESERVATION_STATUSES:
        raise InvalidTreatmentReservationError("治療予約状態は仮予定または確定を指定してください。")

    capacity_dates = [treatment_date for treatment_date in dates if treatment_date not in cancelled_dates]
    if capacity_dates:
        settings = _settings_for_dates(capacity_dates)
        occupancy = _occupancy_for_dates(capacity_dates, exclude_course_id=course.pk)
        capacity_types, snapshots = _allocate_capacity_types(capacity_dates, settings, occupancy)
    else:
        capacity_types, snapshots = [], []
    capacity_type_by_date = dict(zip(capacity_dates, capacity_types))

    plan = InpatientPlan.objects.select_for_update().filter(treatment_course=course).first()
    if plan is not None and (plan.status in {"cancelled", "discharged"} or plan.actual_discharge_at):
        raise InvalidTreatmentReservationError("キャンセル済みまたは退院済みのPlanは予約できません。")
    if create_plan:
        plan_status = "scheduled" if reservation_status == "confirmed" else "provisional"
        if plan is None:
            plan = InpatientPlan.objects.create(
                treatment_course=course,
                status=plan_status,
                planned_admission_date=admission_date,
                created_by=user,
                updated_by=user,
            )
        else:
            if plan.status not in {"admitted", "discharged"}:
                plan.status = plan_status
            plan.planned_admission_date = admission_date
            plan.updated_by = user
            plan.save(update_fields=["status", "planned_admission_date", "updated_by", "updated_at"])

    if update_course:
        course.admission_date = admission_date
        course.first_treatment_date = first_treatment_date
        course.planned_treatment_sessions = 30
        course.save(update_fields=[
            "admission_date", "first_treatment_date", "planned_treatment_sessions", "updated_at",
        ])

    existing_by_sequence = {item.sequence_number: item for item in existing}
    for index, reservation in enumerate(existing):
        reservation.treatment_date = date(1901, 1, 1) + timedelta(days=index)
        reservation.save(update_fields=["treatment_date", "updated_at"])

    reservations = []
    for sequence_number, treatment_date in enumerate(dates, start=1):
        reservation = existing_by_sequence.get(sequence_number)
        if reservation is None:
            reservation = RtmSTreatmentReservation(treatment_course=course)
        reservation.treatment_date = treatment_date
        reservation.sequence_number = sequence_number
        reservation.status = "cancelled" if treatment_date in cancelled_dates else reservation_status
        if treatment_date in capacity_type_by_date:
            reservation.capacity_type = capacity_type_by_date[treatment_date]
        elif sequence_number in existing_by_sequence:
            reservation.capacity_type = existing_by_sequence[sequence_number].capacity_type
        else:
            reservation.capacity_type = CAPACITY_TYPES[0]
        reservation.save()
        reservations.append(reservation)

    if locked_entry is not None:
        locked_entry.status = "provisional"
        locked_entry.inpatient_plan = plan
        locked_entry.save(update_fields=["status", "inpatient_plan", "updated_at"])

    return {
        "course": course,
        "plan": plan,
        "reservations": reservations,
        "treatment_end_date": dates[-1],
        "capacity_snapshots": snapshots,
    }


def create_or_replace_rtms_treatment_reservation(
    *, treatment_course, admission_date: date, first_treatment_date: date,
    treatment_dates=None, cancelled_treatment_dates=None, reservation_status=None,
    waitlist_entry=None, user=None, create_plan=True, update_course=True,
):
    """Atomically reserve 30 treatment dates for one Course."""
    if treatment_dates is None:
        holiday_end = first_treatment_date + timedelta(days=365)
        holiday_dates = get_japanese_holidays(first_treatment_date, holiday_end)
        planned_dates = generate_treatment_dates(
            first_treatment_date, total=30, holidays=holiday_dates,
        )
    else:
        planned_dates = list(treatment_dates)
    if (
        len(planned_dates) != 30
        or planned_dates[0] != first_treatment_date
        or planned_dates != sorted(set(planned_dates))
    ):
        raise InvalidTreatmentReservationError("30件の異なる治療日を昇順で指定してください。")

    def operation():
        course = TreatmentCourse.objects.select_for_update().get(pk=treatment_course.pk)
        _reserve_sqlite_writer(planned_dates[0], planned_dates[-1])
        return _create_or_replace_locked(
            treatment_course=course,
            admission_date=admission_date,
            first_treatment_date=first_treatment_date,
            treatment_dates=planned_dates,
            cancelled_treatment_dates=cancelled_treatment_dates,
            reservation_status=reservation_status,
            waitlist_entry=waitlist_entry,
            user=user,
            create_plan=create_plan,
            update_course=update_course,
        )

    return _run_with_sqlite_busy_retry(operation)


def sync_rtms_treatment_reservation_from_sessions(
    *, treatment_course, user=None, allow_canonical_fallback=False,
):
    """Replace one Course's reservations from its materialized 30-session sequence."""
    def operation():
        course = TreatmentCourse.objects.select_for_update().get(pk=treatment_course.pk)
        sessions = list(
            TreatmentSession.objects.select_for_update().filter(treatment_course=course)
            .order_by("session_date", "pk")[:30]
        )
        if any(
            session.patient_id != course.patient_id
            or session.course_number != course.course_number
            for session in sessions
        ):
            raise InvalidTreatmentReservationError(
                "SessionのPatient/Course所有関係が治療予約と一致しません。"
            )
        invalid_statuses = {session.status for session in sessions} - {"planned", "done", "skipped"}
        if invalid_statuses:
            raise InvalidTreatmentReservationError("Session状態が治療予約へ同期できません。")
        if len(sessions) == 30:
            treatment_dates = [session.session_date for session in sessions]
            return create_or_replace_rtms_treatment_reservation(
                treatment_course=course,
                admission_date=course.admission_date or treatment_dates[0],
                first_treatment_date=treatment_dates[0],
                treatment_dates=treatment_dates,
                cancelled_treatment_dates=[
                    session.session_date for session in sessions if session.status == "skipped"
                ],
                user=user,
                create_plan=False,
                update_course=False,
            )

        reservations = list(
            RtmSTreatmentReservation.objects.select_for_update()
            .filter(treatment_course=course).order_by("sequence_number")
        )
        has_active_reservations = any(
            item.status in ACTIVE_RESERVATION_STATUSES for item in reservations
        )
        if not has_active_reservations:
            return None
        if allow_canonical_fallback and len(reservations) == 30:
            first_treatment_date = course.first_treatment_date
            if first_treatment_date is not None:
                return create_or_replace_rtms_treatment_reservation(
                    treatment_course=course,
                    admission_date=course.admission_date or first_treatment_date,
                    first_treatment_date=first_treatment_date,
                    user=user,
                    create_plan=False,
                    update_course=False,
                )
        if not sessions:
            return None

        reservations_by_sequence = {item.sequence_number: item for item in reservations}
        changed = []
        cancelled = []
        for sequence_number, session in enumerate(sessions, start=1):
            reservation = reservations_by_sequence.get(sequence_number)
            if reservation is None:
                continue
            if session.status == "skipped":
                if reservation.status in ACTIVE_RESERVATION_STATUSES:
                    cancelled.append(reservation)
            elif (
                reservation.status in ACTIVE_RESERVATION_STATUSES
                and reservation.treatment_date != session.session_date
            ):
                changed.append((reservation, session.session_date))

        if not changed and not cancelled:
            return {"course": course, "reservations": reservations, "updated_reservations": []}

        changed_ids = {reservation.pk for reservation, _ in changed}
        final_dates = {
            reservation.treatment_date for reservation in reservations
            if reservation.pk not in changed_ids
        }
        if any(treatment_date in final_dates for _, treatment_date in changed):
            raise InvalidTreatmentReservationError("Course内で治療予約日が重複しています。")

        if changed:
            target_dates = sorted({treatment_date for _, treatment_date in changed})
            settings = _settings_for_dates(target_dates)
            occupancy = _occupancy_for_dates(target_dates, exclude_course_id=course.pk)
            changed_reservations = {reservation.pk for reservation, _ in changed}
            cancelled_reservations = {reservation.pk for reservation in cancelled}
            for reservation in reservations:
                if (
                    reservation.pk not in changed_reservations
                    and reservation.pk not in cancelled_reservations
                    and reservation.status in ACTIVE_RESERVATION_STATUSES
                    and reservation.treatment_date in target_dates
                ):
                    key = (reservation.treatment_date, reservation.capacity_type)
                    occupancy[key] = occupancy.get(key, 0) + 1
            capacity_types, _ = _allocate_capacity_types(target_dates, settings, occupancy)
            capacity_type_by_date = dict(zip(target_dates, capacity_types))

            for index, (reservation, _treatment_date) in enumerate(changed):
                reservation.treatment_date = date(1901, 1, 1) + timedelta(days=index)
                reservation.save(update_fields=["treatment_date", "updated_at"])
            for reservation, treatment_date in changed:
                reservation.treatment_date = treatment_date
                reservation.capacity_type = capacity_type_by_date[treatment_date]
                reservation.save(update_fields=["treatment_date", "capacity_type", "updated_at"])

        for reservation in cancelled:
            reservation.status = "cancelled"
            reservation.save(update_fields=["status", "updated_at"])

        return {
            "course": course,
            "reservations": reservations,
            "updated_reservations": [reservation for reservation, _ in changed] + cancelled,
        }

    return _run_with_sqlite_busy_retry(operation)


def confirm_rtms_treatment_reservation(*, treatment_course, waitlist_entry=None, user=None):
    """Confirm all active treatment reservations without changing clinical sessions."""
    def operation():
        course = TreatmentCourse.objects.select_for_update().get(pk=treatment_course.pk)
        reservations = list(RtmSTreatmentReservation.objects.select_for_update().filter(
            treatment_course=course,
            status__in=ACTIVE_RESERVATION_STATUSES,
        ))
        if len(reservations) != 30:
            raise InvalidTreatmentReservationError("30回分の有効な治療予約が揃っていません。")
        locked_entry = _validate_waitlist_entry(waitlist_entry, course)
        plan = InpatientPlan.objects.select_for_update().filter(treatment_course=course).first()
        if plan is None:
            raise InvalidTreatmentReservationError("入院Planがないため予約を確定できません。")
        if plan.status in {"cancelled", "discharged"} or plan.actual_discharge_at:
            raise InvalidTreatmentReservationError("キャンセル済みまたは退院済みのPlanは確定できません。")

        RtmSTreatmentReservation.objects.filter(
            treatment_course=course,
            status__in=ACTIVE_RESERVATION_STATUSES,
        ).update(status="confirmed", updated_at=timezone.now())
        if plan.status != "admitted":
            plan.status = "scheduled"
            plan.updated_by = user
            plan.save(update_fields=["status", "updated_by", "updated_at"])
        if locked_entry is not None:
            locked_entry.status = "scheduled"
            locked_entry.scheduled_at = timezone.now()
            locked_entry.inpatient_plan = plan
            locked_entry.save(update_fields=[
                "status", "scheduled_at", "inpatient_plan", "updated_at",
            ])
        return {"course": course, "plan": plan, "reservations": reservations}

    return _run_with_sqlite_busy_retry(operation)