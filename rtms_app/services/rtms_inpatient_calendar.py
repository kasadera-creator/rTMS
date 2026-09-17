from calendar import monthrange
from datetime import date, timedelta

from django.db.models import Q

from rtms_app.models import (
    InpatientPlan,
    ResourceAssignment,
    ResourcePool,
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


def _month_bounds(year, month):
    start = date(year, month, 1)
    return start, date(year, month, monthrange(year, month)[1])


def _plan_interval(plan, month_end):
    start = plan.actual_admission_at.date() if plan.actual_admission_at else plan.planned_admission_date
    end, end_kind = resolve_inpatient_end_date(plan)
    return start, end or month_end, end_kind


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
        },
    }


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
        "treatment_course__patient", "resource_pool",
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
            assignment.planned_end_date,
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
                "end_kind": "unknown" if assignment.planned_end_date is None else "known",
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
        timeline = _visible_timeline(course.admission_date, None, month_start, month_end)
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
            overdue = bool(
                end_kind == "planned"
                and end < current
                and plan.actual_discharge_at is None
            )
            display_end = month_end if overdue else end
            if start and start <= current <= display_end:
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
            and (assignment.planned_end_date is None or current <= assignment.planned_end_date)
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
        "label": "rTMS個室が未設定です",
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


def build_inpatient_calendar_range(
    *, start_date, week_count=18, treatment_course=None, resource_pool=None, today=None,
):
    """Build a Course-scoped weekly display without changing stored dates."""
    start_date = start_date - timedelta(days=start_date.weekday())
    weeks = _week_bounds(start_date, week_count)
    period_end = weeks[-1]["end"]
    pool = resource_pool or ResourcePool.objects.filter(
        resource_type="rTMS_private", is_active=True,
    ).order_by("pk").first()
    plans = list(InpatientPlan.objects.filter(
        status__in=ACTIVE_PLAN_STATUSES,
    ).select_related("treatment_course__patient"))
    courses = list(TreatmentCourse.objects.filter(
        Q(admission_date__lte=period_end) | Q(first_treatment_date__lte=period_end),
    ).filter(
        Q(admission_date__isnull=False) | Q(first_treatment_date__isnull=False),
    ).select_related("patient"))
    sessions = TreatmentSession.objects.filter(
        session_date__range=(start_date, period_end),
    ).select_related("treatment_course__patient", "patient")
    assignments = ResourceAssignment.objects.filter(
        status__in=ACTIVE_ASSIGNMENT_STATUSES,
        planned_start_date__lte=period_end,
    ).filter(Q(planned_end_date__isnull=True) | Q(planned_end_date__gte=start_date)).select_related(
        "treatment_course__patient", "resource_pool",
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
        rows_by_course.setdefault(assignment.treatment_course_id, _course_row(assignment.treatment_course))
    for session in sessions:
        if session.treatment_course_id is not None:
            rows_by_course.setdefault(session.treatment_course_id, _course_row(session.treatment_course))

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
        timeline = _week_timeline(course.admission_date, None, start_date, period_end)
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
            assignment.planned_end_date,
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
                "end_kind": "unknown" if assignment.planned_end_date is None else "known",
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

    for row in rows_by_course.values():
        if row["timelines"]["rtms_period"]:
            continue
        planned_start = row["treatment_course"].first_treatment_date
        timeline = _week_timeline(planned_start, planned_start, start_date, period_end)
        if timeline is not None:
            row["timelines"]["rtms_period"].append({
                **timeline,
                "session_count": 0,
                "planned_count": 1,
                "done_count": 0,
                "skipped_count": 0,
                "planned_only": True,
                "title": f"rTMS開始予定：{planned_start:%Y/%m/%d}",
            })

    calendar_rows = sorted(
        rows_by_course.values(),
        key=lambda row: (row["patient"].name, row["course_number"], row["treatment_course_id"]),
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
        )
        private_room_courses = {
            assignment.treatment_course_id
            for assignment in assignments
            if assignment.planned_start_date <= week["end"]
            and (assignment.planned_end_date is None or assignment.planned_end_date >= week["start"])
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
        )
        rtms_courses = {
            session.treatment_course_id
            for session in sessions
            if session.treatment_course_id is not None
            and week["start"] <= session.session_date <= week["end"]
        }
        rtms_courses.update(
            row["treatment_course_id"]
            for row in rows_by_course.values()
            if not row["timelines"]["rtms"]
            and row["treatment_course"].first_treatment_date
            and week["start"] <= row["treatment_course"].first_treatment_date <= week["end"]
        )
        week.update({
            "inpatient_count": len(inpatient_courses),
            "private_room_count": len(private_room_courses),
            "rtms_course_count": len(rtms_courses),
            "is_current": bool(today and week["start"] <= today <= week["end"]),
        })

    return {
        "weekly_calendar_rows": calendar_rows,
        "weeks": weeks,
        "weekly_load": weeks,
        "month_spans": _week_month_spans(weeks),
        "weekly_metrics": [
            {
                "label": "入院者数",
                "weeks": [{"week": week, "value": week["inpatient_count"]} for week in weeks],
            },
            {
                "label": "個室利用者数",
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
