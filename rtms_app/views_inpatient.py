from datetime import date, timedelta
from urllib.parse import urlencode

from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date

from rtms_app.forms_inpatient import (
    CalendarCourseAdjustmentForm,
    InpatientScheduleForm,
    RtmSWaitlistEntryForm,
)
from rtms_app.models import Patient, RtmSWaitlistEntry, TreatmentCourse
from rtms_app.queries.assessment_queries import resolve_treatment_course
from rtms_app.services.inpatient_planning import (
    confirm_inpatient_schedule,
    update_calendar_course_adjustment,
)
from rtms_app.services.resource_capacity import CapacityConflict
from rtms_app.services.rtms_inpatient_calendar import (
    build_inpatient_calendar,
    build_inpatient_calendar_range,
)
from rtms_app.services.waitlist import (
    ACTIVE_WAITLIST_STATUSES,
    WaitlistAlreadyRegisteredError,
    register_waitlist_entry,
)


def _course_for_request(request, patient):
    requested = request.GET.get("course_number") or request.POST.get("course_number")
    course = resolve_treatment_course(patient, course_number=requested)
    if requested and course is None:
        raise ValueError("対象の治療クールが見つかりません")
    return course or TreatmentCourse.objects.filter(
        patient=patient, course_number=patient.course_number or 1,
    ).first()


@login_required
def inpatient_calendar_view(request):
    patient_id = request.GET.get("patient_id")
    treatment_course = None
    patient = None
    if patient_id:
        patient = get_object_or_404(Patient, pk=patient_id)
        try:
            treatment_course = _course_for_request(request, patient)
        except ValueError as exc:
            return HttpResponseBadRequest(str(exc))
    if request.method == "POST" and request.POST.get("action") == "save_calendar_course":
        course_id = request.POST.get("treatment_course_id")
        entry_id = request.POST.get("waitlist_entry_id")
        if not course_id or not course_id.isdigit():
            return HttpResponseBadRequest("対象の治療クールが指定されていません")
        course = get_object_or_404(TreatmentCourse.objects.select_related("patient"), pk=int(course_id))
        if entry_id:
            entry = get_object_or_404(
                RtmSWaitlistEntry,
                pk=entry_id,
                treatment_course=course,
                status__in=ACTIVE_WAITLIST_STATUSES,
            )
        else:
            entry = get_object_or_404(
                RtmSWaitlistEntry,
                treatment_course=course,
                status__in=ACTIVE_WAITLIST_STATUSES,
            )
        form = CalendarCourseAdjustmentForm(request.POST, instance=course)
        if form.is_valid():
            with transaction.atomic():
                update_calendar_course_adjustment(
                    treatment_course=course,
                    user=request.user,
                    admission_date=form.cleaned_data["admission_date"],
                    treatment_start_date=form.cleaned_data["first_treatment_date"],
                    private_room_planned=form.cleaned_data["private_room_planned"],
                    is_all_case_survey=form.cleaned_data["is_all_case_survey"],
                    planned_treatment_sessions=form.cleaned_data["planned_treatment_sessions"],
                )
                entry.preferred_start_note = form.cleaned_data["preferred_start_note"]
                entry.save(update_fields=["preferred_start_note", "updated_at"])
            return redirect(request.get_full_path())
        adjustment_course_id = course.pk
    else:
        adjustment_course_id = None
    today = timezone.localdate()
    try:
        year = int(request.GET.get("year", today.year))
        month = int(request.GET.get("month", today.month))
        date(year, month, 1)
    except (TypeError, ValueError):
        year, month = today.year, today.month
    if month == 1:
        previous_year, previous_month = year - 1, 12
    else:
        previous_year, previous_month = year, month - 1
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1

    def month_url(target_year, target_month):
        query = request.GET.copy()
        query["year"] = target_year
        query["month"] = target_month
        return f"?{urlencode(query, doseq=True)}"
    context = build_inpatient_calendar(
        year=year,
        month=month,
        treatment_course=treatment_course,
    )
    current_week_start = today - timedelta(days=today.weekday())
    context.update(build_inpatient_calendar_range(
        start_date=current_week_start,
        week_count=18,
        treatment_course=treatment_course,
        resource_pool=context["resource_pool"],
        today=today,
    ))
    waitlist_entries = RtmSWaitlistEntry.objects.filter(
        status__in=ACTIVE_WAITLIST_STATUSES,
    ).select_related("treatment_course__patient").order_by(
        "registered_at",
        "treatment_course__patient__name",
        "pk",
    )
    if treatment_course is not None:
        waitlist_entries = waitlist_entries.filter(treatment_course=treatment_course)
    waitlist_entries = list(waitlist_entries)
    for entry in waitlist_entries:
        entry.adjustment_form = CalendarCourseAdjustmentForm(
            instance=entry.treatment_course,
            initial={
                "preferred_start_note": entry.preferred_start_note,
            },
        )
        entry.adjustment_form_open = False
        if entry.treatment_course_id == adjustment_course_id:
            entry.adjustment_form = form
            entry.adjustment_form_open = True
    context.update({
        "patient": patient,
        "treatment_course": treatment_course,
        "waitlist_entries": waitlist_entries,
        "waitlist_active_count": RtmSWaitlistEntry.objects.filter(
            status__in=ACTIVE_WAITLIST_STATUSES,
        ).count(),
        "selected_date": request.GET.get("selected_date"),
        "previous_month_url": month_url(previous_year, previous_month),
        "next_month_url": month_url(next_year, next_month),
        "today_month_url": month_url(today.year, today.month),
    })
    if treatment_course:
        pool = context["resource_pool"]
        context["schedule_form"] = InpatientScheduleForm(resource_pool=pool)
    return render(request, "rtms_app/inpatient_calendar.html", context)


@login_required
def rtms_waitlist_patient_select_view(request):
    """Choose a Patient/Course before entering the existing first-visit flow."""
    card_query = (request.GET.get("card") or "").strip()
    name_query = (request.GET.get("q") or "").strip()
    courses = TreatmentCourse.objects.select_related("patient").order_by(
        "patient__name", "patient__card_id", "course_number", "pk"
    )
    if card_query:
        courses = courses.filter(patient__card_id__icontains=card_query)
    if name_query:
        courses = courses.filter(patient__name__icontains=name_query)
    courses = courses[:100]
    return render(request, "rtms_app/rtms_waitlist_patient_select.html", {
        "courses": courses,
        "card_query": card_query,
        "name_query": name_query,
    })


@login_required
def inpatient_schedule_view(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    try:
        treatment_course = _course_for_request(request, patient)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    if treatment_course is None:
        return HttpResponseBadRequest("対象の治療クールが見つかりません")
    if request.method == "POST":
        form = InpatientScheduleForm(request.POST)
        if form.is_valid():
            try:
                confirm_inpatient_schedule(
                    treatment_course=treatment_course,
                    resource_pool=form.cleaned_data["resource_pool"],
                    admission_date=form.cleaned_data["admission_date"],
                    treatment_start_date=form.cleaned_data["treatment_start_date"],
                    room_start_date=form.cleaned_data["room_start_date"],
                    room_end_date=form.cleaned_data["room_end_date"],
                    planned_discharge_date=form.cleaned_data["planned_discharge_date"],
                    estimated_discharge_date=form.cleaned_data["estimated_discharge_date"],
                    certainty=form.cleaned_data["certainty"],
                    user=request.user,
                )
            except CapacityConflict as exc:
                form.add_error(None, str(exc))
            else:
                return redirect(
                    f"{reverse('rtms_app:inpatient_calendar')}?patient_id={patient.pk}"
                    f"&course_number={treatment_course.course_number}"
                )
    else:
        plan = getattr(treatment_course, "inpatient_plan", None)
        form_pool = None
        selected_date = parse_date(request.GET.get("selected_date", ""))
        if plan:
            defaults = {
                "admission_date": plan.planned_admission_date,
                "planned_discharge_date": plan.planned_discharge_date,
                "estimated_discharge_date": plan.estimated_discharge_date,
            }
        else:
            defaults = {"admission_date": treatment_course.admission_date}
        if selected_date:
            defaults.update({
                "admission_date": selected_date,
                "treatment_start_date": selected_date,
                "room_start_date": selected_date,
            })
        form = InpatientScheduleForm(initial=defaults, resource_pool=form_pool)
    return render(request, "rtms_app/inpatient_schedule.html", {
        "patient": patient,
        "treatment_course": treatment_course,
        "form": form,
    })


@login_required
def rtms_waitlist_view(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    try:
        treatment_course = _course_for_request(request, patient)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    if treatment_course is None:
        return HttpResponseBadRequest("対象の治療クールが見つかりません")
    if request.method == "POST":
        form = RtmSWaitlistEntryForm(request.POST)
        if form.is_valid():
            try:
                with transaction.atomic():
                    locked_course = TreatmentCourse.objects.select_for_update().get(pk=treatment_course.pk)
                    locked_course.planned_treatment_sessions = form.cleaned_data["planned_treatment_sessions"] or 30
                    locked_course.save(update_fields=["planned_treatment_sessions", "updated_at"])
                    register_waitlist_entry(
                        treatment_course=locked_course,
                        user=request.user,
                        status="waiting",
                        **{
                            key: value
                            for key, value in form.cleaned_data.items()
                            if key != "planned_treatment_sessions"
                        },
                    )
            except WaitlistAlreadyRegisteredError as exc:
                form.add_error(None, str(exc))
            else:
                return redirect(
                    f"{reverse('rtms_app:rtms_waitlist', args=[patient.pk])}"
                    f"?course_number={treatment_course.course_number}"
                )
    else:
        form = RtmSWaitlistEntryForm()
    entries = treatment_course.rtms_waitlist_entries.order_by("registered_at", "pk")
    return render(request, "rtms_app/rtms_waitlist.html", {
        "patient": patient,
        "treatment_course": treatment_course,
        "form": form,
        "entries": entries,
    })


@login_required
def rtms_waitlist_management_view(request):
    return redirect("rtms_app:inpatient_calendar")
