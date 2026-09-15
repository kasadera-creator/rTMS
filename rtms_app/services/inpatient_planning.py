from django.db import transaction

from rtms_app.models import InpatientPlan, TreatmentCourse


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
