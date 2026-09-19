from django.db import transaction
from django.utils import timezone

from rtms_app.models import RtmSWaitlistEntry, TreatmentCourse


ACTIVE_WAITLIST_STATUSES = ("requested", "waiting", "provisional")
RESERVED_WAITLIST_STATUSES = ACTIVE_WAITLIST_STATUSES + ("scheduled",)


class WaitlistAlreadyRegisteredError(ValueError):
    """Raised when a Course already has a current waitlist episode."""


@transaction.atomic
def register_waitlist_entry(*, treatment_course: TreatmentCourse, user=None, **values):
    """Create a waitlist episode without creating a resource assignment."""
    locked_course = TreatmentCourse.objects.select_for_update().get(pk=treatment_course.pk)
    if RtmSWaitlistEntry.objects.filter(
        treatment_course=locked_course,
        status__in=RESERVED_WAITLIST_STATUSES,
    ).exists():
        raise WaitlistAlreadyRegisteredError(
            "このCourseはすでにrTMS待機リストに登録されています。"
        )
    return RtmSWaitlistEntry.objects.create(
        treatment_course=locked_course,
        registered_by=user,
        **values,
    )


@transaction.atomic
def mark_waitlist_scheduled(*, entry: RtmSWaitlistEntry, inpatient_plan, user=None):
    locked_entry = RtmSWaitlistEntry.objects.select_for_update().get(pk=entry.pk)
    locked_entry.status = "scheduled"
    locked_entry.scheduled_at = timezone.now()
    locked_entry.inpatient_plan = inpatient_plan
    locked_entry.registered_by = user or locked_entry.registered_by
    locked_entry.save(update_fields=["status", "scheduled_at", "inpatient_plan", "registered_by", "updated_at"])
    return locked_entry
