from __future__ import annotations
import json
import logging
from typing import Dict, Any

from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseForbidden, JsonResponse, HttpRequest, HttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.conf import settings

from rtms_app.models import Patient, PatientSurveySession, PatientSurveyResponse, TreatmentSession
from rtms_app.services.patient_accounts import PATIENT_GROUP_NAME
from rtms_app.surveys import (
    INSTRUMENT_ORDER,
    INSTRUMENT_SET,
    get_instrument,
    instrument_label,
)

logger = logging.getLogger(__name__)

PATIENT_LOGIN_URL = "/patient/login/"


def _is_patient_user(user) -> bool:
    return user.is_authenticated and user.groups.filter(name=PATIENT_GROUP_NAME).exists()


def _get_patient(user):
    if not user.is_authenticated:
        return None
    patient = getattr(user, "patient_profile", None)
    if patient:
        return patient
    return Patient.objects.filter(user=user).first()


def _is_staff_user(user) -> bool:
    """Check if user is staff (has is_staff=True or is in any group other than PATIENT_GROUP_NAME)."""
    if not user.is_authenticated:
        return False
    return user.is_staff or user.is_superuser or (user.groups.exists() and not user.groups.filter(name=PATIENT_GROUP_NAME).exists())


def patient_login(request: HttpRequest):
    if _is_patient_user(request.user):
        return redirect("patient_portal:portal")

    # Check if staff is logged in
    is_staff_logged_in = _is_staff_user(request.user)

    # If staff is accessing via GET with a logout confirmation
    if request.method == "POST" and request.POST.get("action") == "logout_and_login":
        logout(request)
        is_staff_logged_in = False

    error = None
    if request.method == "POST" and request.POST.get("action") != "logout_and_login":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        user = authenticate(request, username=username, password=password)
        if user and _is_patient_user(user):
            logout(request)  # Ensure any staff login is cleared
            login(request, user)
            return redirect("patient_portal:portal")
        error = "ログインに失敗しました。IDとパスワードを確認してください。"

    return render(request, "rtms_app/patient/login.html", {
        "error": error,
        "is_staff_logged_in": is_staff_logged_in,
    })


def patient_logout(request: HttpRequest):
    logout(request)
    return redirect("patient_portal:login")


def _ensure_patient_or_forbid(request: HttpRequest) -> Patient | None:
    if not _is_patient_user(request.user):
        return None
    patient = _get_patient(request.user)
    if not patient:
        return None
    return patient


@login_required(login_url=PATIENT_LOGIN_URL)
def portal(request: HttpRequest):
    patient = _ensure_patient_or_forbid(request)
    if not patient:
        return HttpResponseForbidden("患者専用ページです")

    active_sessions = PatientSurveySession.objects.filter(patient=patient).order_by("-started_at")

    latest_pre = active_sessions.filter(phase="pre").first()
    latest_post = active_sessions.filter(phase="post").first()
    latest_session = active_sessions.first()

    # Treatment progress for recommendation
    done_qs = TreatmentSession.objects.filter(patient=patient, status="done")
    completed_count = done_qs.count()
    first_treatment_date = done_qs.order_by("session_date").values_list("session_date", flat=True).first()
    last_treatment_date = done_qs.order_by("-session_date").values_list("session_date", flat=True).first()

    recommended_phase = None
    recommendation_reason = None
    pre_window_days = getattr(settings, "PATIENT_SURVEY_PRE_WINDOW_DAYS", 7)
    today = timezone.localdate()

    if completed_count >= 20:
        recommended_phase = "post"
        recommendation_reason = f"治療回数が{completed_count}回のため、治療後（介入後）の検査をおすすめします。"
    elif completed_count == 0:
        recommended_phase = "pre"
        recommendation_reason = "治療開始前のため、治療前（介入前）の検査をおすすめします。"
    elif first_treatment_date and abs((today - first_treatment_date).days) <= pre_window_days:
        recommended_phase = "pre"
        recommendation_reason = "初回治療日が近いため、治療前（介入前）の検査をおすすめします。"

    context = {
        "patient": patient,
        "active_sessions": active_sessions,
        "instrument_order": INSTRUMENT_ORDER,
        "latest_session": latest_session,
        "latest_pre": latest_pre,
        "latest_post": latest_post,
        "in_progress_session": active_sessions.filter(status="in_progress").first(),
        "recommended_phase": recommended_phase,
        "recommendation_reason": recommendation_reason,
        "completed_count": completed_count,
        "first_treatment_date": first_treatment_date,
        "last_treatment_date": last_treatment_date,
        "pre_window_days": pre_window_days,
    }
    return render(request, "rtms_app/patient/portal.html", context)


@login_required(login_url=PATIENT_LOGIN_URL)
def start_session(request: HttpRequest):
    patient = _ensure_patient_or_forbid(request)
    if not patient:
        return HttpResponseForbidden("患者専用ページです")
    if request.method != "POST":
        return redirect("patient_portal:portal")

    phase = request.POST.get("phase")
    if phase not in {"pre", "post"}:
        return redirect("patient_portal:portal")

    session, created = PatientSurveySession.objects.get_or_create(
        patient=patient,
        phase=phase,
        status="in_progress",
        defaults={"course_number": getattr(patient, "course_number", 1)},
    )
    if created:
        session.course_number = getattr(patient, "course_number", 1)
        session.save()

    first_code = INSTRUMENT_ORDER[0]
    return redirect("patient_portal:instrument", session_id=session.id, instrument=first_code)


def _extract_answers(request: HttpRequest, instrument_def: Dict[str, Any]) -> Dict[str, Any]:
    answers: Dict[str, Any] = {}
    for q in instrument_def.get("questions", []):
        key = q.get("key")
        if not key:
            continue
        if request.content_type.startswith("application/json"):
            continue
        val = request.POST.get(key)
        if val is not None:
            answers[key] = val
    return answers


def _missing_questions(instrument_def: Dict[str, Any], answers: Dict[str, Any]) -> list[str]:
    missing = []
    for q in instrument_def.get("questions", []):
        key = q.get("key")
        if key and key not in answers:
            missing.append(key)
    return missing


@login_required(login_url=PATIENT_LOGIN_URL)
def instrument_view(request: HttpRequest, session_id: int, instrument: str):
    # Normalize instrument code
    instrument = (instrument or "").strip().lower()
    patient = _ensure_patient_or_forbid(request)
    if not patient:
        return HttpResponseForbidden("患者専用ページです")

    session = get_object_or_404(PatientSurveySession, id=session_id, patient=patient)
    if session.status == "submitted":
        return redirect("patient_portal:review", session_id=session.id)

    if instrument not in INSTRUMENT_SET:
        logger.warning(
            "invalid instrument",
            extra={
                "path": request.path,
                "instrument": instrument,
                "allowed_instruments": list(INSTRUMENT_ORDER),
                "session_id": session_id,
            },
        )
        return HttpResponseForbidden("不正な検査コードです")

    try:
        instrument_def = get_instrument(instrument)
    except KeyError:
        logger.warning(
            "instrument lookup failed",
            extra={
                "instrument": instrument,
                "allowed_instruments": list(INSTRUMENT_ORDER),
            },
        )
        return HttpResponseForbidden("不正な検査コードです")

    response, _ = PatientSurveyResponse.objects.get_or_create(session=session, instrument=instrument, defaults={"answers": {}})

    if request.method == "POST":
        if request.content_type.startswith("application/json"):
            data = json.loads(request.body.decode("utf-8")) if request.body else {}
            answers = data.get("answers", {}) or {}
            nav = data.get("nav") or "stay"
        else:
            answers = _extract_answers(request, instrument_def)
            nav = request.POST.get("nav") or "stay"

        # Basic missing check for navigation actions
        missing = _missing_questions(instrument_def, answers)
        if nav in {"next", "submit"} and missing:
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"missing": missing}, status=400)
            context = {
                "patient": patient,
                "session": session,
                "instrument": instrument,
                "instrument_def": instrument_def,
                "answers": answers,
                "progress": INSTRUMENT_ORDER,
                "current_index": INSTRUMENT_ORDER.index(instrument),
                "error": "未回答の設問があります。",
            }
            return render(request, "rtms_app/patient/instrument.html", context)

        response.answers = answers
        response.save()

        if request.headers.get("x-requested-with") == "XMLHttpRequest":
            return JsonResponse({"total": response.total_score, "extras": response.extra_data})

        idx = INSTRUMENT_ORDER.index(instrument)
        prev_code = INSTRUMENT_ORDER[idx - 1] if idx > 0 else None
        next_code = INSTRUMENT_ORDER[idx + 1] if idx + 1 < len(INSTRUMENT_ORDER) else None

        if nav == "prev" and prev_code:
            logger.info("nav prev", extra={"path": request.path, "instrument": instrument, "prev": prev_code})
            return redirect("patient_portal:instrument", session_id=session.id, instrument=prev_code)

        if nav in {"next", "submit"}:
            logger.info(
                "nav next",
                extra={
                    "path": request.path,
                    "instrument": instrument,
                    "next": next_code,
                    "at_last": next_code is None,
                },
            )
            if next_code:
                return redirect("patient_portal:instrument", session_id=session.id, instrument=next_code)
            return redirect("patient_portal:review", session_id=session.id)

    total_instruments = len(INSTRUMENT_ORDER)
    current_index = INSTRUMENT_ORDER.index(instrument)

    prev_code = INSTRUMENT_ORDER[current_index - 1] if current_index > 0 else None
    next_code = INSTRUMENT_ORDER[current_index + 1] if current_index + 1 < total_instruments else None

    context = {
        "patient": patient,
        "session": session,
        "instrument": instrument,
        "instrument_def": instrument_def,
        "answers": response.answers or {},
        "progress": INSTRUMENT_ORDER,
        "current_index": current_index,
        "current_number": current_index + 1,
        "total_instruments": total_instruments,
        "current_total": response.total_score,
        "next_code": next_code,
        "prev_code": prev_code,
    }
    return render(request, "rtms_app/patient/instrument.html", context)


@login_required(login_url=PATIENT_LOGIN_URL)
def review(request: HttpRequest, session_id: int):
    patient = _ensure_patient_or_forbid(request)
    if not patient:
        return HttpResponseForbidden("患者専用ページです")
    session = get_object_or_404(PatientSurveySession, id=session_id, patient=patient)

    responses = {r.instrument: r for r in session.responses.all()}
    summary = []
    for code in INSTRUMENT_ORDER:
        resp = responses.get(code)
        summary.append({
            "code": code,
            "label": instrument_label(code),
            "total": resp.total_score if resp else None,
            "phq9_q10": resp.phq9_difficulty if resp else None,
        })

    missing_instruments = [code for code in INSTRUMENT_ORDER if code not in responses]
    return render(
        request,
        "rtms_app/patient/review.html",
        {
            "patient": patient,
            "session": session,
            "summary": summary,
            "missing_instruments": missing_instruments,
            "is_submitted": session.status == "submitted",
        },
    )


@login_required(login_url=PATIENT_LOGIN_URL)
def submit(request: HttpRequest, session_id: int):
    patient = _ensure_patient_or_forbid(request)
    if not patient:
        return HttpResponseForbidden("患者専用ページです")
    session = get_object_or_404(PatientSurveySession, id=session_id, patient=patient)

    # Ensure all instruments answered
    for code in INSTRUMENT_ORDER:
        try:
            inst_def = get_instrument(code)
        except KeyError:
            continue
        resp, _ = PatientSurveyResponse.objects.get_or_create(session=session, instrument=code, defaults={"answers": {}})
        answers = resp.answers or {}
        missing = _missing_questions(inst_def, answers)
        if missing:
            return redirect("patient_portal:instrument", session_id=session.id, instrument=code)

    session.status = "submitted"
    session.submitted_at = timezone.now()
    session.save(update_fields=["status", "submitted_at"])
    return redirect("patient_portal:review", session_id=session.id)
