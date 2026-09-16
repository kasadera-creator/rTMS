from calendar import monthrange
from datetime import date, timedelta

from django.db.models import Q

from rtms_app.models import (
    InpatientPlan,
    ResourceAssignment,
    ResourcePool,
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


def build_inpatient_calendar(*, year, month, treatment_course=None, resource_pool=None):
    month_start, month_end = _month_bounds(year, month)
    pool = resource_pool or ResourcePool.objects.filter(
        resource_type="rTMS_private", is_active=True,
    ).order_by("pk").first()
    plans = list(InpatientPlan.objects.filter(
        status__in=ACTIVE_PLAN_STATUSES,
    ).select_related("treatment_course__patient"))
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
        sessions = sessions.filter(treatment_course=treatment_course)
        assignments = assignments.filter(treatment_course=treatment_course)

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
        "resource_pool": pool,
        "forecast": forecast,
        "summary": {
            "peak_inpatients": max((day["inpatient_count"] for day in days), default=0),
            "peak_rtms": max((day["rtms_count"] for day in days), default=0),
            "peak_private_rooms": max((day["private_room_count"] for day in days), default=0),
        },
    }
