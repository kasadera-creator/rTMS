from datetime import date

from django.contrib.auth.decorators import login_required
from django.http import HttpResponseBadRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date

from rtms_app.forms_inpatient import InpatientScheduleForm, RtmSWaitlistEntryForm
from rtms_app.models import Patient, TreatmentCourse
from rtms_app.queries.assessment_queries import resolve_treatment_course
from rtms_app.services.inpatient_planning import confirm_inpatient_schedule
from rtms_app.services.resource_capacity import CapacityConflict
from rtms_app.services.rtms_inpatient_calendar import build_inpatient_calendar
from rtms_app.services.waitlist import register_waitlist_entry


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
    today = timezone.localdate()
    try:
        year = int(request.GET.get("year", today.year))
        month = int(request.GET.get("month", today.month))
        date(year, month, 1)
    except (TypeError, ValueError):
        year, month = today.year, today.month
    context = build_inpatient_calendar(
        year=year,
        month=month,
        treatment_course=treatment_course,
    )
    context.update({
        "patient": patient,
        "treatment_course": treatment_course,
        "selected_date": request.GET.get("selected_date"),
    })
    if treatment_course:
        pool = context["resource_pool"]
        context["schedule_form"] = InpatientScheduleForm(resource_pool=pool)
    return render(request, "rtms_app/inpatient_calendar.html", context)


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
            register_waitlist_entry(
                treatment_course=treatment_course,
                user=request.user,
                status="waiting",
                **form.cleaned_data,
            )
            return redirect(
                f"{reverse('rtms_app:rtms_waitlist')}?patient_id={patient.pk}"
                f"&course_number={treatment_course.course_number}"
            )
    else:
        form = RtmSWaitlistEntryForm()
    entries = treatment_course.rtms_waitlist_entries.order_by("-priority", "-registered_at")
    return render(request, "rtms_app/rtms_waitlist.html", {
        "patient": patient,
        "treatment_course": treatment_course,
        "form": form,
        "entries": entries,
    })
