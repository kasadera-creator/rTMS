from datetime import date, timedelta

from django.db import transaction
from django.db.models import Q

from rtms_app.models import (
    InpatientPlan,
    RtmSAdmissionCapacity,
    ResourceAssignment,
    ResourcePool,
    TreatmentCourse,
)


ACTIVE_ASSIGNMENT_STATUSES = ("planned", "active")
ACTIVE_PLAN_STATUSES = ("draft", "provisional", "scheduled", "admitted")


class CapacityConflict(ValueError):
    """Raised when a hard physical or admission capacity would be exceeded."""


def admission_capacity_for_date(target_date: date, *, for_update=False):
    query = RtmSAdmissionCapacity.objects.filter(
        is_active=True,
        valid_from__lte=target_date,
    ).filter(Q(valid_to__isnull=True) | Q(valid_to__gte=target_date))
    if query.count() > 1:
        raise CapacityConflict("対象日に有効なrTMS新規入院受入枠が複数あります")
    if for_update:
        query = query.select_for_update()
    return query.order_by("-valid_from", "-pk").first()


def admission_count(start_date: date, end_date: date | None = None, *, exclude_course_id=None):
    end_date = end_date or start_date
    query = InpatientPlan.objects.filter(status__in=ACTIVE_PLAN_STATUSES).filter(
        Q(actual_admission_at__date__range=(start_date, end_date))
        | Q(
            actual_admission_at__isnull=True,
            planned_admission_date__range=(start_date, end_date),
        )
    )
    if exclude_course_id is not None:
        query = query.exclude(treatment_course_id=exclude_course_id)
    return query.values("treatment_course_id").distinct().count()


def overlapping_assignments(resource_pool, start_date: date, end_date: date | None = None, *, exclude_assignment_id=None):
    """Return non-cancelled assignments whose planned period overlaps the range."""
    if end_date is not None and end_date < start_date:
        raise ValueError("planned_end_date must be on or after planned_start_date")

    query = ResourceAssignment.objects.filter(
        resource_pool=resource_pool,
        status__in=ACTIVE_ASSIGNMENT_STATUSES,
        planned_start_date__lte=end_date if end_date is not None else date.max,
    ).filter(
        Q(planned_end_date__isnull=True)
        | Q(planned_end_date__gte=start_date)
    )
    if exclude_assignment_id is not None:
        query = query.exclude(pk=exclude_assignment_id)
    return query


def capacity_snapshot(resource_pool, start_date: date, end_date: date | None = None, *, exclude_assignment_id=None):
    """Return capacity and overlapping planned-assignment counts."""
    assignments = overlapping_assignments(
        resource_pool, start_date, end_date, exclude_assignment_id=exclude_assignment_id,
    )
    count = assignments.values("treatment_course_id").distinct().count()
    return {
        "resource_pool_id": resource_pool.pk,
        "physical_capacity": resource_pool.physical_capacity,
        "operational_target": resource_pool.operational_target,
        "planned_count": count,
        "physical_capacity_available": count < resource_pool.physical_capacity,
        "operational_target_available": count < resource_pool.operational_target,
        "operational_target_warning": count >= resource_pool.operational_target,
    }


def ensure_capacity(resource_pool, start_date: date, end_date: date | None = None):
    """Raise ValueError when a planned assignment would exceed physical capacity."""
    snapshot = capacity_snapshot(resource_pool, start_date, end_date)
    if not snapshot["physical_capacity_available"]:
        raise ValueError("ResourcePool physical capacity would be exceeded")
    return snapshot


def schedule_capacity_snapshot(
    resource_pool,
    admission_date: date,
    room_start_date: date,
    room_end_date: date | None = None,
    *,
    exclude_assignment_id=None,
    exclude_course_id=None,
):
    room = capacity_snapshot(
        resource_pool, room_start_date, room_end_date,
        exclude_assignment_id=exclude_assignment_id,
    )
    setting = admission_capacity_for_date(admission_date)
    current_admissions = admission_count(
        setting.valid_from,
        setting.valid_to or admission_date,
        exclude_course_id=exclude_course_id,
    ) if setting else 0
    admission_limit = setting.capacity if setting else None
    return {
        **room,
        "admission_capacity": admission_limit,
        "admission_count": current_admissions,
        "admission_capacity_available": (
            setting is not None and current_admissions < admission_limit
        ),
        "admission_capacity_configured": setting is not None,
        "admission_capacity_setting": setting,
    }


def ensure_schedule_capacity(
    resource_pool,
    admission_date: date,
    room_start_date: date,
    room_end_date: date | None = None,
    *,
    exclude_assignment_id=None,
    exclude_course_id=None,
):
    snapshot = schedule_capacity_snapshot(
        resource_pool, admission_date, room_start_date, room_end_date,
        exclude_assignment_id=exclude_assignment_id,
        exclude_course_id=exclude_course_id,
    )
    if not snapshot["physical_capacity_available"]:
        raise CapacityConflict("個室の物理容量を超えるため、日程を確定できません")
    if not snapshot["admission_capacity_configured"]:
        raise CapacityConflict("対象日のrTMS新規入院受入枠が設定されていません")
    if not snapshot["admission_capacity_available"]:
        raise CapacityConflict("rTMS新規入院受入枠を超えるため、日程を確定できません")
    return snapshot


def earliest_admission_forecast(start_date: date, *, room_days=14, resource_pool=None, horizon_days=370):
    pools = [resource_pool] if resource_pool is not None else list(
        ResourcePool.objects.filter(resource_type="rTMS_private", is_active=True)
    )
    if not pools:
        return {"date": None, "label": "受入可能枠未設定", "uncertainty_count": 0}
    unknown_count = InpatientPlan.objects.filter(
        status__in=ACTIVE_PLAN_STATUSES,
        actual_discharge_at__isnull=True,
        planned_discharge_date__isnull=True,
        estimated_discharge_date__isnull=True,
    ).count()
    for offset in range(horizon_days + 1):
        candidate = start_date + timedelta(days=offset)
        if admission_capacity_for_date(candidate) is None:
            continue
        room_end = candidate + timedelta(days=max(room_days - 1, 0))
        for pool in pools:
            snapshot = schedule_capacity_snapshot(pool, candidate, candidate, room_end)
            if snapshot["physical_capacity_available"] and snapshot["admission_capacity_available"]:
                return {
                    "date": candidate,
                    "label": f"{candidate.month}月{candidate.day}日から受け入れ可能見込み",
                    "uncertainty_count": unknown_count,
                    "operational_warning": snapshot["operational_target_warning"],
                }
    return {"date": None, "label": "受入可能時期を算出できません", "uncertainty_count": unknown_count}


@transaction.atomic
def create_planned_assignment(
    *,
    treatment_course: TreatmentCourse,
    resource_pool: ResourcePool,
    planned_start_date: date,
    planned_end_date: date | None = None,
    status: str = "planned",
    certainty: str = "confirmed",
    reason: str = "",
    user=None,
):
    """Create one assignment with Course/pool locks and an atomic capacity check."""
    locked_course = TreatmentCourse.objects.select_for_update().get(pk=treatment_course.pk)
    locked_pool = ResourcePool.objects.select_for_update().get(pk=resource_pool.pk)
    ensure_capacity(locked_pool, planned_start_date, planned_end_date)
    return ResourceAssignment.objects.create(
        treatment_course=locked_course,
        resource_pool=locked_pool,
        planned_start_date=planned_start_date,
        planned_end_date=planned_end_date,
        status=status,
        certainty=certainty,
        reason=reason,
        created_by=user,
        updated_by=user,
    )


def resolve_inpatient_end_date(plan: InpatientPlan):
    """Resolve a display/forecast end date without changing any stored date."""
    if plan.actual_discharge_at:
        return plan.actual_discharge_at.date(), "actual"
    if plan.planned_discharge_date:
        return plan.planned_discharge_date, "planned"
    if plan.estimated_discharge_date:
        return plan.estimated_discharge_date, "estimated"
    return None, "unknown"
