from datetime import date, timedelta
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from rtms_app.forms_inpatient import (
    CalendarCourseAdjustmentForm,
    InpatientScheduleForm,
    RtmSWaitlistEntryForm,
)
from rtms_app.models import Patient, ResourceAssignment, RtmSWaitlistEntry, TreatmentCourse
from rtms_app.utils.request_context import get_client_ip, get_user_agent
from rtms_app.views import (
    get_course_inpatient_display_state,
    get_patient_admission_status,
)
from rtms_app.queries.assessment_queries import resolve_treatment_course
from rtms_app.services.inpatient_planning import (
    ActiveResourceAssignmentError,
    ActualAdmissionError,
    ActualDischargeError,
    can_rollback_provisional_schedule,
    confirm_inpatient_schedule,
    InvalidWaitlistEntryError,
    ProvisionalRollbackError,
    ScheduleConfirmationBusyError,
    record_actual_admission,
    record_actual_discharge,
    rollback_provisional_schedule,
    update_calendar_course_adjustment,
)
from rtms_app.services.resource_capacity import CapacityConflict
from rtms_app.services.rtms_treatment_reservations import (
    InvalidTreatmentReservationError,
    TreatmentCapacityConfigurationError,
    TreatmentReservationBusyError,
    TreatmentReservationCapacityConflict,
    preview_rtms_treatment_reservation,
)
from rtms_app.services.rtms_inpatient_calendar import (
    add_new_course_start_options,
    build_inpatient_calendar,
    build_inpatient_calendar_range,
)
from rtms_app.services.waitlist import (
    ACTIVE_WAITLIST_STATUSES,
    WaitlistAlreadyRegisteredError,
    register_waitlist_entry,
)

WAITLIST_DISPLAY_STATUSES = ("requested", "waiting")


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
    selected_booking_waitlist_id = ""
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
            selected_booking_waitlist_id = entry.pk
        else:
            entry = None
        form = CalendarCourseAdjustmentForm(request.POST, instance=course)
        if form.is_valid():
            try:
                with transaction.atomic():
                    update_calendar_course_adjustment(
                        treatment_course=course,
                        user=request.user,
                        admission_date=form.cleaned_data["admission_date"],
                        treatment_start_date=form.cleaned_data["first_treatment_date"],
                        is_all_case_survey=form.cleaned_data["is_all_case_survey"],
                        planned_treatment_sessions=30,
                        waitlist_entry=entry,
                    )
                    if entry is not None:
                        entry.preferred_start_note = form.cleaned_data["preferred_start_note"]
                        entry.save(update_fields=["preferred_start_note", "updated_at"])
            except (
                ValueError,
                InvalidTreatmentReservationError,
                TreatmentCapacityConfigurationError,
                TreatmentReservationBusyError,
                TreatmentReservationCapacityConflict,
            ) as exc:
                form.add_error(None, str(exc))
                adjustment_course_id = course.pk
            else:
                return redirect(request.get_full_path())
        else:
            adjustment_course_id = course.pk
    else:
        adjustment_course_id = None
    today = timezone.localdate()
    calendar_week_count = 12
    current_week_start = today - timedelta(days=today.weekday())
    range_start = parse_date(request.GET.get("week_start", "")) or current_week_start
    range_start -= timedelta(days=range_start.weekday())
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
    context.update(build_inpatient_calendar_range(
        start_date=range_start,
        week_count=calendar_week_count,
        resource_pool=context["resource_pool"],
        today=today,
    ))
    add_new_course_start_options(context["weeks"])
    waitlist_entries = RtmSWaitlistEntry.objects.filter(
        status__in=WAITLIST_DISPLAY_STATUSES,
    ).select_related("treatment_course__patient").order_by(
        "registered_at",
        "treatment_course__patient__name",
        "pk",
    )
    waitlist_entries = list(waitlist_entries)
    provisional_entries_by_course = {
        entry.treatment_course_id: entry
        for entry in RtmSWaitlistEntry.objects.filter(
            status="provisional",
        ).select_related("treatment_course__patient")
    }
    for row in context["treatment_calendar_rows"]:
        row["provisional_waitlist_entry"] = provisional_entries_by_course.get(
            row["treatment_course_id"],
        )
        row["can_rollback_provisional"] = bool(
            row["provisional_waitlist_entry"]
            and can_rollback_provisional_schedule(
                treatment_course=row["treatment_course"],
                waitlist_entry=row["provisional_waitlist_entry"],
            )
        )
    range_query = request.GET.copy()
    range_query["week_start"] = (range_start - timedelta(weeks=1)).isoformat()
    previous_week_url = f"?{urlencode(range_query, doseq=True)}"
    range_query["week_start"] = (range_start + timedelta(weeks=1)).isoformat()
    next_week_url = f"?{urlencode(range_query, doseq=True)}"
    range_query["week_start"] = (range_start - timedelta(weeks=calendar_week_count)).isoformat()
    previous_range_url = f"?{urlencode(range_query, doseq=True)}"
    range_query["week_start"] = (range_start + timedelta(weeks=calendar_week_count)).isoformat()
    next_range_url = f"?{urlencode(range_query, doseq=True)}"
    range_query["week_start"] = current_week_start.isoformat()
    today_range_url = f"?{urlencode(range_query, doseq=True)}"
    booking_courses = TreatmentCourse.objects.select_related("patient").order_by(
        "patient__name", "patient__card_id", "course_number", "pk",
    )
    selected_booking_course_id = (
        form.instance.pk if request.method == "POST" and "form" in locals()
        else treatment_course.pk if treatment_course else ""
    )
    context.update({
        "patient": patient,
        "treatment_course": treatment_course,
        "waitlist_entries": waitlist_entries,
        "booking_courses": booking_courses,
        "calendar_form": (
            form if request.method == "POST" and "form" in locals()
            else CalendarCourseAdjustmentForm(
                instance=treatment_course,
                initial={"planned_treatment_sessions": 30},
            )
        ),
        "selected_booking_course_id": selected_booking_course_id,
        "selected_booking_waitlist_id": selected_booking_waitlist_id,
        "previous_week_url": previous_week_url,
        "next_week_url": next_week_url,
        "previous_range_url": previous_range_url,
        "next_range_url": next_range_url,
        "today_range_url": today_range_url,
        "range_preview_url": reverse("rtms_app:rtms_reservation_preview"),
        "waitlist_display_count": len(waitlist_entries),
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
@require_POST
def rollback_provisional_waitlist_view(request):
    course_id = request.POST.get("course_id", "")
    entry_id = request.POST.get("waitlist_entry_id", "")
    if not course_id.isdigit() or not entry_id.isdigit():
        return HttpResponseBadRequest("対象Courseと待機リストが指定されていません")
    course = get_object_or_404(TreatmentCourse.objects.select_related("patient"), pk=course_id)
    entry = get_object_or_404(RtmSWaitlistEntry, pk=entry_id)
    try:
        rollback_provisional_schedule(
            patient_id=course.patient_id,
            treatment_course=course,
            waitlist_entry=entry,
            user=request.user,
            ip=get_client_ip(request),
            user_agent=get_user_agent(request),
        )
    except ProvisionalRollbackError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "待機状態に戻しました。")
    calendar_url = reverse("rtms_app:inpatient_calendar")
    return redirect(
        f"{calendar_url}?patient_id={course.patient_id}"
        f"&course_number={course.course_number}"
    )


@login_required
def rtms_reservation_preview_view(request):
    first_treatment_date = parse_date(request.GET.get("first_treatment_date", ""))
    if first_treatment_date is None:
        return JsonResponse({"error": "初回治療日を入力してください。"}, status=400)
    course_id = request.GET.get("treatment_course_id", "")
    try:
        exclude_course_id = int(course_id) if course_id else None
        preview = preview_rtms_treatment_reservation(
            first_treatment_date=first_treatment_date,
            exclude_course_id=exclude_course_id,
        )
    except (ValueError, TreatmentCapacityConfigurationError) as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    return JsonResponse({
        "treatment_end_date": preview["treatment_end_date"].isoformat(),
        "treatment_count": len(preview["treatment_dates"]),
        "conflicts": [
            {
                "date": item["date"].isoformat(),
                "private": item["rTMS_private"],
                "general": item["general_ward"],
                "total_capacity": item["total_capacity"],
                "total_occupied": item["total_occupied"],
            }
            for item in preview["conflicts"]
        ],
    })


@login_required
def rtms_waitlist_patient_select_view(request):
    """Choose a Patient/Course before entering the existing first-visit flow."""
    card_query = (request.GET.get("card") or "").strip()
    name_query = (request.GET.get("q") or "").strip()
    courses = TreatmentCourse.objects.select_related("patient", "inpatient_plan").order_by(
        "patient__name", "patient__card_id", "course_number", "pk"
    )
    if card_query:
        courses = courses.filter(patient__card_id__icontains=card_query)
    if name_query:
        courses = courses.filter(patient__name__icontains=name_query)
    courses = courses[:100]
    course_rows = [
        {
            "course": course,
            "status": get_patient_admission_status(
                course.patient,
                treatment_course=course,
            ),
            **get_course_inpatient_display_state(course),
        }
        for course in courses
    ]
    return render(request, "rtms_app/rtms_waitlist_patient_select.html", {
        "course_rows": course_rows,
        "card_query": card_query,
        "name_query": name_query,
    })


@login_required
@require_POST
def record_actual_admission_view(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    requested_course_number = request.POST.get("course_number")
    if not requested_course_number:
        return HttpResponseBadRequest("対象の治療クールが指定されていません")
    treatment_course = resolve_treatment_course(
        patient,
        course_number=requested_course_number,
    )
    if treatment_course is None:
        return HttpResponseBadRequest("対象の治療クールが見つかりません")

    try:
        result = record_actual_admission(
            treatment_course=treatment_course,
            user=request.user,
        )
    except ActualAdmissionError as exc:
        messages.error(request, str(exc))
    else:
        if result["already_recorded"]:
            messages.info(request, "実入院実績はすでに登録されています。登録日時は変更していません。")
        else:
            messages.success(request, "実入院を記録しました。")

    schedule_url = reverse("rtms_app:inpatient_schedule", args=[patient.pk])
    return redirect(
        f"{schedule_url}?patient_id={patient.pk}"
        f"&course_number={treatment_course.course_number}"
    )


@login_required
@require_POST
def record_actual_discharge_view(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    requested_course_number = request.POST.get("course_number")
    if not requested_course_number:
        return HttpResponseBadRequest("対象の治療クールが指定されていません")
    treatment_course = resolve_treatment_course(
        patient,
        course_number=requested_course_number,
    )
    if treatment_course is None:
        return HttpResponseBadRequest("対象の治療クールが見つかりません")

    try:
        result = record_actual_discharge(
            treatment_course=treatment_course,
            user=request.user,
        )
    except ActualDischargeError as exc:
        messages.error(request, str(exc))
    else:
        if result["already_recorded"]:
            messages.info(request, "実退院実績はすでに登録されています。登録日時は変更していません。")
        else:
            messages.success(request, "実退院を記録しました。")

    schedule_url = reverse("rtms_app:inpatient_schedule", args=[patient.pk])
    return redirect(
        f"{schedule_url}?patient_id={patient.pk}"
        f"&course_number={treatment_course.course_number}"
    )


@login_required
def inpatient_schedule_view(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    waitlist_confirmation = (
        request.GET.get("waitlist_confirmation") == "1"
        or request.POST.get("waitlist_confirmation") == "1"
    )
    if waitlist_confirmation and (
        request.GET.get("waitlist_confirmation") != "1"
        or not request.GET.get("course_number")
        or (
            request.method == "POST"
            and (
                request.POST.get("waitlist_confirmation") != "1"
                or request.POST.get("course_number") != request.GET.get("course_number")
            )
        )
    ):
        return HttpResponseBadRequest("対象Courseを確認できません")
    try:
        treatment_course = _course_for_request(request, patient)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    if treatment_course is None:
        return HttpResponseBadRequest("対象の治療クールが見つかりません")
    inpatient_plan = getattr(treatment_course, "inpatient_plan", None)
    confirmation_entry = None
    if waitlist_confirmation:
        confirmation_entry = treatment_course.rtms_waitlist_entries.filter(
            status__in=ACTIVE_WAITLIST_STATUSES,
        ).select_related("treatment_course").order_by("registered_at", "pk").first()
        if confirmation_entry is None or confirmation_entry.status not in {"waiting", "provisional"}:
            return HttpResponseBadRequest("このCourseに確定可能な待機登録がありません")
        if request.method == "GET" and (
            treatment_course.admission_date is None
            or treatment_course.first_treatment_date is None
        ):
            return HttpResponseBadRequest("先に初診画面で日程候補を保存してください")
    if request.method == "POST":
        form = InpatientScheduleForm(request.POST)
        if form.is_valid():
            if waitlist_confirmation:
                waitlist_entry = confirmation_entry
            else:
                waitlist_entry = RtmSWaitlistEntry.objects.filter(
                    treatment_course=treatment_course,
                    status__in=("waiting", "provisional"),
                ).order_by("-priority", "registered_at", "pk").first()
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
                    waitlist_entry=waitlist_entry,
                )
            except (
                CapacityConflict,
                ActiveResourceAssignmentError,
                InvalidWaitlistEntryError,
                InvalidTreatmentReservationError,
                ScheduleConfirmationBusyError,
                TreatmentCapacityConfigurationError,
                TreatmentReservationCapacityConflict,
            ) as exc:
                form.add_error(None, str(exc))
            else:
                return redirect(
                    f"{reverse('rtms_app:inpatient_calendar')}?patient_id={patient.pk}"
                    f"&course_number={treatment_course.course_number}"
                )
    else:
        assignment = ResourceAssignment.objects.filter(
            treatment_course=treatment_course,
            status__in=("planned", "active"),
        ).select_related("resource_pool").order_by("-pk").first()
        form_pool = assignment.resource_pool if assignment else None
        selected_date = parse_date(request.GET.get("selected_date", ""))
        if inpatient_plan:
            defaults = {
                "admission_date": inpatient_plan.planned_admission_date,
                "treatment_start_date": treatment_course.first_treatment_date,
                "room_start_date": assignment.planned_start_date if assignment else inpatient_plan.planned_admission_date,
                "room_end_date": assignment.planned_end_date if assignment else None,
                "planned_discharge_date": inpatient_plan.planned_discharge_date,
                "estimated_discharge_date": inpatient_plan.estimated_discharge_date,
                "certainty": (
                    assignment.certainty if assignment
                    else "provisional" if inpatient_plan.uncertainty == "provisional"
                    else "estimated" if inpatient_plan.uncertainty == "unknown"
                    else "confirmed"
                ),
            }
        else:
            defaults = {
                "admission_date": treatment_course.admission_date,
                "treatment_start_date": treatment_course.first_treatment_date,
                "room_start_date": treatment_course.admission_date,
            }
        if selected_date:
            defaults.update({
                "admission_date": selected_date,
                "treatment_start_date": selected_date,
                "room_start_date": selected_date,
            })
        if waitlist_confirmation:
            defaults.update({
                "admission_date": treatment_course.admission_date,
                "treatment_start_date": treatment_course.first_treatment_date,
                "room_start_date": defaults.get("room_start_date") or treatment_course.admission_date,
            })
        form = InpatientScheduleForm(initial=defaults, resource_pool=form_pool)
    return render(request, "rtms_app/inpatient_schedule.html", {
        "patient": patient,
        "treatment_course": treatment_course,
        "form": form,
        "inpatient_plan": inpatient_plan,
        "waitlist_confirmation": waitlist_confirmation,
        "waitlist_entry": confirmation_entry,
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
    entries = list(
        RtmSWaitlistEntry.objects.filter(
            status__in=WAITLIST_DISPLAY_STATUSES,
        )
        .select_related("treatment_course__patient")
        .order_by("registered_at", "treatment_course__patient__name", "pk")
    )
    return render(request, "rtms_app/rtms_waitlist_management.html", {
        "entries": entries,
        "active_count": len(entries),
    })
