from django.db import transaction

from rtms_app.models import RtmSWaitlistEntry, TreatmentCourse


@transaction.atomic
def register_waitlist_entry(*, treatment_course: TreatmentCourse, user=None, **values):
    """Create a waitlist episode without creating a resource assignment."""
    locked_course = TreatmentCourse.objects.select_for_update().get(pk=treatment_course.pk)
    return RtmSWaitlistEntry.objects.create(
        treatment_course=locked_course,
        registered_by=user,
        **values,
    )
