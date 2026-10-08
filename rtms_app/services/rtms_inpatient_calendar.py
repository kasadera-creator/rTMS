from calendar import monthrange
from datetime import date, timedelta
import logging

from django.db.models import Count, Q
from django.utils import timezone

from rtms_app.models import (
    InpatientPlan,
    ResourceAssignment,
    ResourcePool,
    RtmSTreatmentReservation,
    RtmSWaitlistEntry,
    TreatmentCourse,
    TreatmentSession,
)
from rtms_app.services.resource_capacity import (
    ACTIVE_ASSIGNMENT_STATUSES,
    ACTIVE_PLAN_STATUSES,
    admission_capacity_for_date,
    earliest_admission_forecast,
    resolve_inpatient_end_date,
)
from rtms_app.services.rtms_treatment_reservations import (
    ACTIVE_RESERVATION_STATUSES,
    estimate_available_new_courses_by_start_dates,
    treatment_capacity_snapshots,
)
from rtms_app.services.waitlist import ACTIVE_WAITLIST_STATUSES
from rtms_app.services.rtms_schedule import (
    generate_course_treatment_dates,
    get_japanese_holidays,
    is_closed,
    next_open_day,
)

logger = logging.getLogger(__name__)

ACTIVE_SCHEDULE_COURSE_STATUSES = (
    "waiting_admission",
    "inpatient_waiting_treatment",
    "treatment_in_progress",
    "treatment_finished_waiting_discharge",
    "on_hold",
)


def add_new_course_start_options(weeks):
    if not weeks:
        return
    holidays = get_japanese_holidays(
        weeks[0]["start"], weeks[-1]["end"] + timedelta(days=365),
    )
    for week in weeks:
        first_treatment_date = next_open_day(week["start"], holidays)
        if first_treatment_date > week["end"]:
            first_treatment_date = None
        if first_treatment_date is None:
            admission_date = None
        else:
            days_since_friday = (first_treatment_date.weekday() - 4) % 7 or 7
            admission_date = first_treatment_date - timedelta(days=days_since_friday)
        capacity = week["new_course_start_capacity"]
        week.update({
            "first_treatment_date": first_treatment_date,
            "admission_date": admission_date,
            "selectable": capacity is not None and capacity > 0 and first_treatment_date is not None,
        })


def _month_bounds(year, month):
    start = date(year, month, 1)
    return start, date(year, month, monthrange(year, month)[1])


def _plan_interval(plan, month_end):
    start = plan.actual_admission_at.date() if plan.actual_admission_at else plan.planned_admission_date
    end, end_kind = resolve_inpatient_end_date(plan)
    return start, end or month_end, end_kind


def _course_end_date(course):
    """Use the Course discharge date for legacy rows without an InpatientPlan."""
    return course.discharge_date


def _assignment_end_date(assignment):
    """Do not display a room assignment beyond its Course's planned inpatient end."""
    assignment_end = assignment.planned_end_date
    plan = getattr(assignment.treatment_course, "inpatient_plan", None)
    plan_end = resolve_inpatient_end_date(plan)[0] if plan else None
    if plan_end and (assignment_end is None or plan_end < assignment_end):
        return plan_end
    return assignment_end


def _visible_timeline(start, end, month_start, month_end):
    if start is None:
        return None
    visible_end = end or month_end
    if visible_end < month_start or start > month_end:
        return None
    visible_start = max(start, month_start)
    visible_end = min(visible_end, month_end)
    return {
        "start": start,
        "end": end,
        "visible_start": visible_start,
        "visible_end": visible_end,
        "start_day": (visible_start - month_start).days + 1,
        "end_day": (visible_end - month_start).days + 1,
        "span": (visible_end - visible_start).days + 1,
    }


def _course_row(treatment_course):
    return {
        "treatment_course_id": treatment_course.pk,
        "patient_id": treatment_course.patient_id,
        "course_number": treatment_course.course_number,
        "patient": treatment_course.patient,
        "treatment_course": treatment_course,
        "timelines": {
            "inpatient": [],
            "private_room": [],
            "rtms": [],
            "rtms_period": [],
            "rtms_reservation": [],
        },
        "rtms_planned_period": None,
    }


def _has_visible_rtms_bar(row):
    timelines = row["timelines"]
    return bool(
        row["rtms_planned_period"]
        or timelines["rtms_period"]
        or timelines["rtms_reservation"]
    )


def build_inpatient_calendar(*, year, month, treatment_course=None, resource_pool=None):
    month_start, month_end = _month_bounds(year, month)
    pool = resource_pool or ResourcePool.objects.filter(
        resource_type="rTMS_private", is_active=True,
    ).order_by("pk").first()
    plans = list(InpatientPlan.objects.filter(
        status__in=ACTIVE_PLAN_STATUSES,
    ).select_related("treatment_course__patient"))
    courses = list(TreatmentCourse.objects.filter(
        Q(admission_date__lte=month_end) | Q(first_treatment_date__lte=month_end),
    ).filter(
        Q(admission_date__isnull=False) | Q(first_treatment_date__isnull=False),
    ).select_related("patient"))
    sessions = TreatmentSession.objects.filter(
        session_date__range=(month_start, month_end),
    ).select_related("treatment_course__patient", "patient")
    assignments = ResourceAssignment.objects.filter(
        status__in=ACTIVE_ASSIGNMENT_STATUSES,
        planned_start_date__lte=month_end,
    ).filter(Q(planned_end_date__isnull=True) | Q(planned_end_date__gte=month_start)).select_related(
        "treatment_course__patient", "treatment_course__inpatient_plan", "resource_pool",
    )
    if treatment_course is not None:
        plans = [plan for plan in plans if plan.treatment_course_id == treatment_course.pk]
        courses = [treatment_course]
        sessions = sessions.filter(treatment_course=treatment_course)
        assignments = assignments.filter(treatment_course=treatment_course)
    assignments = list(assignments)
    assigned_course_ids = {assignment.treatment_course_id for assignment in assignments}

    rows_by_course = {}
    if treatment_course is not None:
        rows_by_course[treatment_course.pk] = _course_row(treatment_course)
    for course in courses:
        rows_by_course.setdefault(course.pk, _course_row(course))
    for plan in plans:
        rows_by_course.setdefault(plan.treatment_course_id, _course_row(plan.treatment_course))
    for assignment in assignments:
        rows_by_course.setdefault(
            assignment.treatment_course_id,
            _course_row(assignment.treatment_course),
        )
    for session in sessions:
        if session.treatment_course_id is not None:
            rows_by_course.setdefault(
                session.treatment_course_id,
                _course_row(session.treatment_course),
            )

    for plan in plans:
        start = plan.actual_admission_at.date() if plan.actual_admission_at else plan.planned_admission_date
        end, end_kind = resolve_inpatient_end_date(plan)
        timeline = _visible_timeline(start, end, month_start, month_end)
        if timeline is not None:
            rows_by_course[plan.treatment_course_id]["timelines"]["inpatient"].append({
                **timeline,
                "end_kind": end_kind,
                "status": plan.status,
                "plan": plan,
            })
    for assignment in assignments:
        timeline = _visible_timeline(
            assignment.planned_start_date,
            _assignment_end_date(assignment),
            month_start,
            month_end,
        )
        if timeline is not None:
            rows_by_course[assignment.treatment_course_id]["timelines"]["private_room"].append({
                **timeline,
                "status": assignment.status,
                "certainty": assignment.certainty,
                "resource_pool": assignment.resource_pool,
                "assignment": assignment,
                "planned_only": False,
                "end_kind": "unknown" if _assignment_end_date(assignment) is None else "known",
            })
    for plan in plans:
        if plan.treatment_course.private_room_planned and plan.treatment_course_id not in assigned_course_ids:
            start = plan.actual_admission_at.date() if plan.actual_admission_at else plan.planned_admission_date
            end, end_kind = resolve_inpatient_end_date(plan)
            timeline = _visible_timeline(start, end, month_start, month_end)
            if timeline is not None:
                rows_by_course[plan.treatment_course_id]["timelines"]["private_room"].append({
                    **timeline,
                    "status": "planned",
                    "certainty": "provisional",
                    "resource_pool": None,
                    "assignment": None,
                    "planned_only": True,
                    "end_kind": end_kind,
                })
    plan_course_ids = {plan.treatment_course_id for plan in plans}
    for course in courses:
        if course.pk in plan_course_ids or not course.admission_date:
            continue
        timeline = _visible_timeline(
            course.admission_date,
            _course_end_date(course),
            month_start,
            month_end,
        )
        if timeline is not None:
            rows_by_course[course.pk]["timelines"]["inpatient"].append({
                **timeline,
                "end_kind": "unknown",
                "status": "draft",
                "plan": None,
            })
            if course.private_room_planned and course.pk not in assigned_course_ids:
                rows_by_course[course.pk]["timelines"]["private_room"].append({
                    **timeline,
                    "status": "planned",
                    "certainty": "provisional",
                    "resource_pool": None,
                    "assignment": None,
                    "planned_only": True,
                    "end_kind": "unknown",
                })
    for session in sessions:
        if session.treatment_course_id is not None:
            rows_by_course[session.treatment_course_id]["timelines"]["rtms"].append({
                "date": session.session_date,
                "start": session.session_date,
                "end": session.session_date,
                "visible_start": session.session_date,
                "visible_end": session.session_date,
                "start_day": (session.session_date - month_start).days + 1,
                "end_day": (session.session_date - month_start).days + 1,
                "span": 1,
                "status": session.status,
                "session": session,
                "session_id": session.pk,
            })

    calendar_rows = sorted(
        rows_by_course.values(),
        key=lambda row: (row["patient"].name, row["course_number"], row["treatment_course_id"]),
    )

    days = []
    current = month_start
    while current <= month_end:
        day_plans = []
        for plan in plans:
            start, end, end_kind = _plan_interval(plan, month_end)
            overdue = False
            if start and start <= current <= end:
                day_plans.append({
                    "course": plan.treatment_course,
                    "plan": plan,
                    "end_kind": end_kind,
                    "unknown_end": end_kind == "unknown",
                    "overdue": overdue,
                })
        day_sessions = [session for session in sessions if session.session_date == current]
        day_assignments = [assignment for assignment in assignments if (
            assignment.planned_start_date <= current
            and (
                _assignment_end_date(assignment) is None
                or current <= _assignment_end_date(assignment)
            )
        )]
        day_private_room_courses = {assignment.treatment_course_id for assignment in day_assignments}
        day_assignments.extend(
            plan for plan in plans
            if plan.treatment_course.private_room_planned
            and plan.treatment_course_id not in assigned_course_ids
            and plan.treatment_course_id not in day_private_room_courses
            and (
                (plan.actual_admission_at.date() if plan.actual_admission_at else plan.planned_admission_date)
                and (plan.actual_admission_at.date() if plan.actual_admission_at else plan.planned_admission_date) <= current
                and (
                    resolve_inpatient_end_date(plan)[0] is None
                    or resolve_inpatient_end_date(plan)[0] >= current
                )
            )
        )
        setting = admission_capacity_for_date(current)
        day = {
            "date": current,
            "inpatient": day_plans,
            "inpatient_count": len(day_plans),
            "rtms": list(day_sessions),
            "rtms_count": len(day_sessions),
            "private_room": day_assignments,
            "private_room_count": len({item.treatment_course_id for item in day_assignments}),
            "physical_capacity": pool.physical_capacity if pool else None,
            "operational_target": pool.operational_target if pool else None,
            "admission_capacity": setting.capacity if setting else None,
            "admission_capacity_configured": setting is not None,
            "unknown_end_count": sum(item["unknown_end"] for item in day_plans),
            "overdue_count": sum(item["overdue"] for item in day_plans),
        }
        day["operational_warning"] = bool(
            pool and day["private_room_count"] >= pool.operational_target
        )
        days.append(day)
        current += timedelta(days=1)

    forecast = earliest_admission_forecast(month_start, resource_pool=pool) if pool else {
        "date": None,
        "label": "rTMS特別個室が未設定です",
        "uncertainty_count": 0,
    }
    return {
        "year": year,
        "month": month,
        "month_start": month_start,
        "month_end": month_end,
        "days": days,
        "days_in_month": len(days),
        "calendar_rows": calendar_rows,
        "resource_pool": pool,
        "forecast": forecast,
        "summary": {
            "peak_inpatients": max((day["inpatient_count"] for day in days), default=0),
            "peak_rtms": max((day["rtms_count"] for day in days), default=0),
            "peak_private_rooms": max((day["private_room_count"] for day in days), default=0),
        },
    }


def _week_bounds(start_date, week_count):
    return [
        {
            "start": start_date + timedelta(days=offset * 7),
            "end": start_date + timedelta(days=offset * 7 + 6),
            "label": (
                f"{(start_date + timedelta(days=offset * 7)).month}/"
                f"{(start_date + timedelta(days=offset * 7)).day}週"
            ),
        }
        for offset in range(week_count)
    ]


def _week_timeline(start, end, period_start, period_end):
    if start is None:
        return None
    visible_end = end or period_end
    if visible_end < period_start or start > period_end:
        return None
    visible_start = max(start, period_start)
    visible_end = min(visible_end, period_end)
    return {
        "start": start,
        "end": end,
        "visible_start": visible_start,
        "visible_end": visible_end,
        "start_week": ((visible_start - period_start).days // 7) + 1,
        "end_week": ((visible_end - period_start).days // 7) + 1,
        "span": ((visible_end - period_start).days // 7)
        - ((visible_start - period_start).days // 7)
        + 1,
    }


def _sum_known_capacity(values):
    return None if any(value is None for value in values) else sum(values)

def _week_month_spans(weeks):
    spans = []
    for index, week in enumerate(weeks, start=1):
        month_key = (week["start"].year, week["start"].month)
        if spans and spans[-1]["key"] == month_key:
            spans[-1]["span"] += 1
        else:
            spans.append({
                "key": month_key,
                "label": f"{week['start'].year}年{week['start'].month}月",
                "start_week": index,
                "span": 1,
            })
    return spans


def _weekly_rtms_course_counts(weeks, reservations, sessions, calendar_rows):
    scheduled_course_dates = {
        (reservation.treatment_course_id, reservation.treatment_date)
        for reservation in reservations
    }
    scheduled_course_dates.update(
        (session.treatment_course_id, session.session_date)
        for session in sessions
        if session.treatment_course_id is not None
        and session.status in {"planned", "done"}
    )
    scheduled_course_dates.update(
        (row["treatment_course_id"], treatment_date)
        for row in calendar_rows
        for treatment_date in row.get("rtms_planned_dates", [])
    )

    course_ids_by_week = {week["start"]: set() for week in weeks}
    period_start = weeks[0]["start"]
    period_end = weeks[-1]["end"]
    for course_id, treatment_date in scheduled_course_dates:
        if course_id is None or not period_start <= treatment_date <= period_end:
            continue
        week_index = (treatment_date - period_start).days // 7
        course_ids_by_week[weeks[week_index]["start"]].add(course_id)
    return {week_start: len(course_ids) for week_start, course_ids in course_ids_by_week.items()}


def build_inpatient_calendar_range(
    *, start_date, week_count=18, treatment_course=None, resource_pool=None, today=None,
):
    """Build a Course-scoped weekly display without changing stored dates."""
    start_date = start_date - timedelta(days=start_date.weekday())
    weeks = _week_bounds(start_date, week_count)
    period_end = weeks[-1]["end"]
    new_course_capacity_by_week = estimate_available_new_courses_by_start_dates(
        week["start"] for week in weeks
    )
    pool = resource_pool or ResourcePool.objects.filter(
        resource_type="rTMS_private", is_active=True,
    ).order_by("pk").first()
    plans = list(InpatientPlan.objects.filter(
        status__in=ACTIVE_PLAN_STATUSES,
    ).select_related("treatment_course__patient"))
    courses = list(TreatmentCourse.objects.filter(
        Q(admission_date__lte=period_end)
        | Q(first_treatment_date__lte=period_end)
        | Q(
            admission_date__isnull=True,
            first_treatment_date__isnull=True,
            course_status__in=ACTIVE_SCHEDULE_COURSE_STATUSES,
        ),
    ).select_related("patient"))
    sessions = TreatmentSession.objects.filter(
        session_date__range=(start_date, period_end),
    ).select_related("treatment_course__patient", "patient")
    assignments = ResourceAssignment.objects.filter(
        status__in=ACTIVE_ASSIGNMENT_STATUSES,
        planned_start_date__lte=period_end,
    ).filter(Q(planned_end_date__isnull=True) | Q(planned_end_date__gte=start_date)).select_related(
        "treatment_course__patient", "treatment_course__inpatient_plan", "resource_pool",
    )
    reservations_in_period = RtmSTreatmentReservation.objects.filter(
        treatment_date__range=(start_date, period_end),
        status__in=ACTIVE_RESERVATION_STATUSES,
    ).select_related("treatment_course__patient")
    if treatment_course is not None:
        plans = [plan for plan in plans if plan.treatment_course_id == treatment_course.pk]
        courses = [treatment_course]
        sessions = sessions.filter(treatment_course=treatment_course)
        assignments = assignments.filter(treatment_course=treatment_course)
        reservations_in_period = reservations_in_period.filter(treatment_course=treatment_course)
    assignments = list(assignments)
    reservations_in_period = list(reservations_in_period)
    assigned_course_ids = {assignment.treatment_course_id for assignment in assignments}

    rows_by_course = {}
    if treatment_course is not None:
        rows_by_course[treatment_course.pk] = _course_row(treatment_course)
    for course in courses:
        rows_by_course.setdefault(course.pk, _course_row(course))
    for plan in plans:
        rows_by_course.setdefault(plan.treatment_course_id, _course_row(plan.treatment_course))
    for assignment in assignments:
        rows_by_course.setdefault(assignment.treatment_course_id, _course_row(assignment.treatment_course))
    for session in sessions:
        if session.treatment_course_id is not None:
            rows_by_course.setdefault(session.treatment_course_id, _course_row(session.treatment_course))
    for reservation in reservations_in_period:
        rows_by_course.setdefault(
            reservation.treatment_course_id,
            _course_row(reservation.treatment_course),
        )
    waitlist_entries = RtmSWaitlistEntry.objects.filter(
        status__in=ACTIVE_WAITLIST_STATUSES,
    ).select_related("treatment_course__patient")
    for entry in waitlist_entries:
        rows_by_course.setdefault(
            entry.treatment_course_id,
            _course_row(entry.treatment_course),
        )

    for plan in plans:
        start = plan.actual_admission_at.date() if plan.actual_admission_at else plan.planned_admission_date
        end, end_kind = resolve_inpatient_end_date(plan)
        timeline = _week_timeline(start, end, start_date, period_end)
        if timeline is not None:
            rows_by_course[plan.treatment_course_id]["timelines"]["inpatient"].append({
                **timeline,
                "end_kind": end_kind,
                "status": plan.status,
                "plan": plan,
            })
            if plan.treatment_course.private_room_planned and plan.treatment_course_id not in assigned_course_ids:
                rows_by_course[plan.treatment_course_id]["timelines"]["private_room"].append({
                    **timeline,
                    "status": "planned",
                    "certainty": "provisional",
                    "resource_pool": None,
                    "assignment": None,
                    "planned_only": True,
                })
    plan_course_ids = {plan.treatment_course_id for plan in plans}
    for course in courses:
        if course.pk in plan_course_ids or not course.admission_date:
            continue
        timeline = _week_timeline(
            course.admission_date,
            _course_end_date(course),
            start_date,
            period_end,
        )
        if timeline is not None:
            rows_by_course[course.pk]["timelines"]["inpatient"].append({
                **timeline,
                "end_kind": "unknown",
                "status": "draft",
                "plan": None,
            })
            if course.private_room_planned and course.pk not in assigned_course_ids:
                rows_by_course[course.pk]["timelines"]["private_room"].append({
                    **timeline,
                    "status": "planned",
                    "certainty": "provisional",
                    "resource_pool": None,
                    "assignment": None,
                    "planned_only": True,
                    "end_kind": "unknown",
                })
    for assignment in assignments:
        timeline = _week_timeline(
            assignment.planned_start_date,
            _assignment_end_date(assignment),
            start_date,
            period_end,
        )
        if timeline is not None:
            rows_by_course[assignment.treatment_course_id]["timelines"]["private_room"].append({
                **timeline,
                "status": assignment.status,
                "certainty": assignment.certainty,
                "resource_pool": assignment.resource_pool,
                "assignment": assignment,
                "planned_only": False,
                "end_kind": "unknown" if _assignment_end_date(assignment) is None else "known",
            })
    for session in sessions:
        if session.treatment_course_id is not None:
            session_week = ((session.session_date - start_date).days // 7) + 1
            rows_by_course[session.treatment_course_id]["timelines"]["rtms"].append({
                "date": session.session_date,
                "start": session.session_date,
                "end": session.session_date,
                "visible_start": session.session_date,
                "visible_end": session.session_date,
                "start_week": session_week,
                "end_week": session_week,
                "span": 1,
                "status": session.status,
                "session": session,
                "session_id": session.pk,
            })

    row_course_ids = [row["treatment_course_id"] for row in rows_by_course.values()]
    all_sessions_by_course = {}
    session_counts_by_course = {}
    completed_session_course_ids = set()
    for session in TreatmentSession.objects.filter(
        treatment_course_id__in=row_course_ids,
    ).order_by("treatment_course_id", "session_date", "pk"):
        all_sessions_by_course.setdefault(session.treatment_course_id, []).append(session)
        session_counts_by_course[session.treatment_course_id] = (
            session_counts_by_course.get(session.treatment_course_id, 0) + 1
        )
        if session.status == "done":
            completed_session_course_ids.add(session.treatment_course_id)
    session_course_ids = set(all_sessions_by_course)
    sessions_by_course = {}
    for session in sessions:
        if session.treatment_course_id is not None:
            sessions_by_course.setdefault(session.treatment_course_id, []).append(session)
    for course_id, course_sessions in sessions_by_course.items():
        first_date = min(session.session_date for session in course_sessions)
        last_date = max(session.session_date for session in course_sessions)
        timeline = _week_timeline(first_date, last_date, start_date, period_end)
        if timeline is not None:
            status_counts = {
                status: sum(session.status == status for session in course_sessions)
                for status in ("planned", "done", "skipped")
            }
            rows_by_course[course_id]["timelines"]["rtms_period"].append({
                **timeline,
                "session_count": len(course_sessions),
                "planned_count": status_counts["planned"],
                "done_count": status_counts["done"],
                "skipped_count": status_counts["skipped"],
                "title": (
                    f"rTMS：予定{status_counts['planned']}回 / "
                    f"実施{status_counts['done']}回 / "
                    f"スキップ{status_counts['skipped']}回"
                ),
            })

    course_reservations = list(RtmSTreatmentReservation.objects.filter(
        treatment_course_id__in=row_course_ids,
        status__in=ACTIVE_RESERVATION_STATUSES,
    ).select_related("treatment_course__patient").order_by("sequence_number"))
    reservations_by_course = {}
    reservations_by_date = {}
    for reservation in course_reservations:
        reservations_by_course.setdefault(reservation.treatment_course_id, []).append(reservation)
        if start_date <= reservation.treatment_date <= period_end:
            reservations_by_date.setdefault(reservation.treatment_date, []).append(reservation)
    status_labels = {"provisional": "仮予定", "confirmed": "確定"}
    for row in rows_by_course.values():
        course = row["treatment_course"]
        course_id = row["treatment_course_id"]
        course_reservations_for_row = reservations_by_course.get(row["treatment_course_id"], [])
        active_statuses = {item.status for item in course_reservations_for_row}
        reservation_status = next(iter(active_statuses), None) if len(active_statuses) == 1 else None
        if len(active_statuses) > 1:
            logger.warning(
                "Course %s has mixed active rTMS Reservation statuses: %s",
                course_id,
                sorted(active_statuses),
            )
        first_reservation = next(
            (item for item in course_reservations_for_row if item.sequence_number == 1),
            None,
        )
        last_reservation = next(
            (item for item in course_reservations_for_row if item.sequence_number == 30),
            None,
        )
        inpatient_plan = next(
            (plan for plan in plans if plan.treatment_course_id == course_id),
            None,
        )
        if course_reservations_for_row and not (
            course.admission_date and course.first_treatment_date
        ):
            logger.warning(
                "Course %s has active rTMS Reservations but incomplete schedule dates",
                course_id,
            )
        if inpatient_plan and (inpatient_plan.status == "admitted") != bool(
            inpatient_plan.actual_admission_at
        ):
            logger.warning(
                "Course %s has inconsistent actual admission fields",
                course_id,
            )
        actual_admission_recorded = bool(
            inpatient_plan
            and inpatient_plan.status == "admitted"
            and inpatient_plan.actual_admission_at
            and timezone.localtime(inpatient_plan.actual_admission_at).date()
            <= (today or timezone.localdate())
        )
        execution_started = bool(
            course.is_admission_procedure_done
            or actual_admission_recorded
            or course_id in completed_session_course_ids
        )
        if execution_started:
            schedule_status = "completed"
        elif course.admission_date and course.first_treatment_date:
            schedule_status = "scheduled"
        else:
            schedule_status = "waiting"
        row.update({
            "reservation_status": reservation_status,
            "reservation_count": len(course_reservations_for_row),
            "reservation_start": first_reservation.treatment_date if first_reservation else None,
            "reservation_end": last_reservation.treatment_date if last_reservation else None,
            "admission_date": row["treatment_course"].admission_date,
            "first_treatment_date": row["treatment_course"].first_treatment_date,
            "treatment_end_date": last_reservation.treatment_date if last_reservation else None,
            "materialized_session_count": session_counts_by_course.get(course_id, 0),
            "planned_treatment_sessions": course.planned_treatment_sessions,
            "schedule_status": schedule_status,
            "schedule_status_label": {
                "waiting": "待機",
                "scheduled": "予定",
                "completed": "確定（実施済）",
            }[schedule_status],
            "inpatient_plan": inpatient_plan,
            "resource_assignments": [
                assignment for assignment in assignments
                if assignment.treatment_course_id == row["treatment_course_id"]
            ],
        })
        if first_reservation and last_reservation:
            reservation_timeline = _week_timeline(
                first_reservation.treatment_date,
                last_reservation.treatment_date,
                start_date,
                period_end,
            )
            if reservation_timeline:
                reservation_timeline.update({
                    "status": reservation_status,
                    "session_count": len(course_reservations_for_row),
                    "title": (
                        f"治療予約30回：{first_reservation.treatment_date:%Y/%m/%d}〜"
                        f"{last_reservation.treatment_date:%Y/%m/%d} / "
                        f"{status_labels.get(reservation_status, '予約')}"
                    ),
                })
                row["timelines"]["rtms_reservation"].append(reservation_timeline)

        planned_start = course.first_treatment_date
        if planned_start is None:
            continue
        treatment_dates = generate_course_treatment_dates(planned_start)
        if not treatment_dates:
            continue
        reservations_by_sequence = {
            item.sequence_number: item for item in course_reservations_for_row
        }
        course_sessions = all_sessions_by_course.get(course_id, [])
        sessions_by_sequence = {
            sequence_number: session
            for sequence_number, session in enumerate(course_sessions, start=1)
        }
        actual_dates = {
            session.session_date for session in course_sessions
        } | {
            reservation.treatment_date for reservation in course_reservations_for_row
        }
        derived_dates = []
        effective_dates = set()
        for sequence_number, planned_date in enumerate(treatment_dates, start=1):
            session = sessions_by_sequence.get(sequence_number)
            reservation = reservations_by_sequence.get(sequence_number)
            if session and session.status == "skipped":
                effective_date = session.session_date
            elif reservation:
                effective_date = reservation.treatment_date
            elif session:
                effective_date = session.session_date
            else:
                effective_date = planned_date
                if effective_date not in actual_dates:
                    derived_dates.append(effective_date)
            effective_dates.add(effective_date)
        for sequence_number, session in enumerate(course_sessions, start=1):
            if sequence_number > len(treatment_dates):
                effective_dates.add(session.session_date)
        for reservation in course_reservations_for_row:
            if reservation.sequence_number > len(treatment_dates):
                effective_dates.add(reservation.treatment_date)
        row["rtms_planned_dates"] = derived_dates
        if not derived_dates or not effective_dates:
            continue
        planned_timeline = _week_timeline(
            min(effective_dates),
            max(effective_dates),
            start_date,
            period_end,
        )
        if planned_timeline is not None:
            row["rtms_planned_period"] = {
                **planned_timeline,
                "treatment_count": len(effective_dates),
            }

    calendar_rows = sorted(
        rows_by_course.values(),
        key=lambda row: (row["patient"].name, row["course_number"], row["treatment_course_id"]),
    )
    treatment_calendar_rows = [row for row in calendar_rows if _has_visible_rtms_bar(row)]
    rtms_course_counts_by_week = _weekly_rtms_course_counts(
        weeks,
        reservations_in_period,
        sessions,
        calendar_rows,
    )

    for week in weeks:
        inpatient_courses = {
            plan.treatment_course_id
            for plan in plans
            if (
                (plan.actual_admission_at.date() if plan.actual_admission_at else plan.planned_admission_date)
                and (plan.actual_admission_at.date() if plan.actual_admission_at else plan.planned_admission_date) <= week["end"]
                and (resolve_inpatient_end_date(plan)[0] is None or resolve_inpatient_end_date(plan)[0] >= week["start"])
            )
        }
        inpatient_courses.update(
            course.pk
            for course in courses
            if course.pk not in plan_course_ids
            and course.admission_date
            and course.admission_date <= week["end"]
            and (
                _course_end_date(course) is None
                or _course_end_date(course) >= week["start"]
            )
        )
        private_room_courses = {
            assignment.treatment_course_id
            for assignment in assignments
            if assignment.planned_start_date <= week["end"]
            and (
                _assignment_end_date(assignment) is None
                or _assignment_end_date(assignment) >= week["start"]
            )
        }
        private_room_courses.update(
            plan.treatment_course_id
            for plan in plans
            if plan.treatment_course.private_room_planned
            and plan.treatment_course_id not in assigned_course_ids
            and (
                (plan.actual_admission_at.date() if plan.actual_admission_at else plan.planned_admission_date)
                and (plan.actual_admission_at.date() if plan.actual_admission_at else plan.planned_admission_date) <= week["end"]
                and (resolve_inpatient_end_date(plan)[0] is None or resolve_inpatient_end_date(plan)[0] >= week["start"])
            )
        )
        private_room_courses.update(
            course.pk
            for course in courses
            if course.pk not in plan_course_ids
            and course.private_room_planned
            and course.admission_date
            and course.admission_date <= week["end"]
            and (
                _course_end_date(course) is None
                or _course_end_date(course) >= week["start"]
            )
        )
        week.update({
            "inpatient_count": len(inpatient_courses),
            "private_room_count": len(private_room_courses),
            "rtms_course_count": rtms_course_counts_by_week[week["start"]],
            "new_course_start_capacity": new_course_capacity_by_week[week["start"]],
            "is_current": bool(today and week["start"] <= today <= week["end"]),
        })

    capacity_by_date = {
        snapshot["date"]: snapshot
        for snapshot in treatment_capacity_snapshots(
            (start_date + timedelta(days=offset) for offset in range(week_count * 7)),
            allow_unconfigured=True,
        )
    }
    holiday_dates = get_japanese_holidays(start_date, period_end)
    weekday_labels = ("月", "火", "水", "木", "金", "土", "日")
    for week in weeks:
        week["days"] = []
        for offset in range(7):
            treatment_date = week["start"] + timedelta(days=offset)
            snapshot = capacity_by_date[treatment_date]
            week["days"].append({
                "date": treatment_date,
                "weekday_label": weekday_labels[offset],
                "is_weekend": offset >= 5,
                "is_today": bool(today and treatment_date == today),
                "private_capacity": snapshot["rTMS_private"],
                "general_capacity": snapshot["general_ward"],
                "total_capacity": snapshot["total_capacity"],
                "total_occupied": snapshot["total_occupied"],
                "total_remaining": snapshot["total_remaining"],
                "reservations": reservations_by_date.get(treatment_date, []),
            })
        operating_days = [
            day for day in week["days"]
            if not is_closed(day["date"], holiday_dates)
        ]
        daily_capacities = {day["total_capacity"] for day in operating_days}
        daily_capacity_breakdowns = {
            (day["private_capacity"]["capacity"], day["general_capacity"]["capacity"])
            for day in operating_days
        }
        daily_capacity = next(iter(daily_capacities)) if len(daily_capacities) == 1 else None
        daily_capacity_breakdown = (
            next(iter(daily_capacity_breakdowns))
            if len(daily_capacity_breakdowns) == 1
            else None
        )
        if daily_capacity_breakdown and any(
            capacity is None for capacity in daily_capacity_breakdown
        ):
            daily_capacity_breakdown = None
        week["treatment_capacity"] = {
            "operating_day_count": len(operating_days),
            "occupied": sum(day["total_occupied"] for day in operating_days),
            "capacity": _sum_known_capacity(
                [day["total_capacity"] for day in operating_days]
            ),
            "daily_capacity": daily_capacity,
            "daily_capacity_breakdown": daily_capacity_breakdown,
            "private": {
                key: (
                    sum(day["private_capacity"][key] for day in operating_days)
                    if key == "occupied"
                    else _sum_known_capacity([
                        day["private_capacity"][key] for day in operating_days
                    ])
                )
                for key in ("occupied", "capacity", "remaining")
            },
            "general": {
                key: (
                    sum(day["general_capacity"][key] for day in operating_days)
                    if key == "occupied"
                    else _sum_known_capacity([
                        day["general_capacity"][key] for day in operating_days
                    ])
                )
                for key in ("occupied", "capacity", "remaining")
            },
        }

    return {
        "weekly_calendar_rows": calendar_rows,
        "treatment_calendar_rows": treatment_calendar_rows,
        "weeks": weeks,
        "weekly_load": weeks,
        "month_spans": _week_month_spans(weeks),
        "weekly_metrics": [
            {
                "label": "入院者数",
                "weeks": [{"week": week, "value": week["inpatient_count"]} for week in weeks],
            },
            {
                "label": "特別個室利用者数",
                "weeks": [{"week": week, "value": week["private_room_count"]} for week in weeks],
            },
            {
                "label": "rTMS治療者数",
                "weeks": [{"week": week, "value": week["rtms_course_count"]} for week in weeks],
            },
        ],
        "weekly_summary": {
            "peak_inpatients": max((week["inpatient_count"] for week in weeks), default=0),
            "peak_private_rooms": max((week["private_room_count"] for week in weeks), default=0),
            "peak_rtms_courses": max((week["rtms_course_count"] for week in weeks), default=0),
        },
        "weekly_resource_pool": pool,
        "week_count": len(weeks),
        "period_start": start_date,
        "period_end": period_end,
    }
