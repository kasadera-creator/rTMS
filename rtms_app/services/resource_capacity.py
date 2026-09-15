from datetime import date

from django.db import transaction
from django.db.models import Q

from rtms_app.models import (
    InpatientPlan,
    ResourceAssignment,
    ResourcePool,
    TreatmentCourse,
)


ACTIVE_ASSIGNMENT_STATUSES = ("planned", "active")


def overlapping_assignments(resource_pool, start_date: date, end_date: date | None = None):
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
    return query


def capacity_snapshot(resource_pool, start_date: date, end_date: date | None = None):
    """Return capacity and overlapping planned-assignment counts."""
    assignments = overlapping_assignments(resource_pool, start_date, end_date)
    count = assignments.count()
    return {
        "resource_pool_id": resource_pool.pk,
        "physical_capacity": resource_pool.physical_capacity,
        "operational_target": resource_pool.operational_target,
        "planned_count": count,
        "physical_capacity_available": count < resource_pool.physical_capacity,
        "operational_target_available": count < resource_pool.operational_target,
    }


def ensure_capacity(resource_pool, start_date: date, end_date: date | None = None):
    """Raise ValueError when a planned assignment would exceed physical capacity."""
    snapshot = capacity_snapshot(resource_pool, start_date, end_date)
    if not snapshot["physical_capacity_available"]:
        raise ValueError("ResourcePool physical capacity would be exceeded")
    return snapshot


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
