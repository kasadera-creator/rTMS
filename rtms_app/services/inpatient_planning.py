from django.db import transaction
from django.utils import timezone

from rtms_app.models import (
    InpatientPlan,
    ResourceAssignment,
    ResourcePool,
    RtmSWaitlistEntry,
    TreatmentCourse,
)
from rtms_app.services.resource_capacity import ensure_schedule_capacity


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
    private_room_planned, is_all_case_survey, planned_treatment_sessions=30, user=None,
):
    """Persist calendar controls through the Course and its existing plan."""
    locked_course = TreatmentCourse.objects.select_for_update().get(pk=treatment_course.pk)
    locked_course.admission_date = admission_date
    locked_course.first_treatment_date = treatment_start_date
    locked_course.private_room_planned = private_room_planned
    locked_course.is_all_case_survey = is_all_case_survey
    locked_course.planned_treatment_sessions = planned_treatment_sessions
    locked_course.save(update_fields=[
        "admission_date",
        "first_treatment_date",
        "private_room_planned",
        "is_all_case_survey",
        "planned_treatment_sessions",
        "updated_at",
    ])
    plan = get_or_create_inpatient_plan(treatment_course=locked_course, user=user)
    plan.planned_admission_date = admission_date
    plan.updated_by = user
    plan.save(update_fields=["planned_admission_date", "updated_by", "updated_at"])
    return {"course": locked_course, "plan": plan}


@transaction.atomic
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
    plan.status = "scheduled"
    plan.planned_admission_date = admission_date
    plan.planned_discharge_date = planned_discharge_date
    plan.estimated_discharge_date = estimated_discharge_date
    plan.uncertainty = "confirmed" if certainty == "confirmed" else "provisional"
    plan.updated_by = user
    plan.save()

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

    if waitlist_entry is not None:
        locked_entry = RtmSWaitlistEntry.objects.select_for_update().get(pk=waitlist_entry.pk)
        locked_entry.status = "scheduled"
        locked_entry.scheduled_at = timezone.now()
        locked_entry.inpatient_plan = plan
        locked_entry.save(update_fields=["status", "scheduled_at", "inpatient_plan", "updated_at"])

    return {"plan": plan, "assignment": assignment, "capacity": capacity}
