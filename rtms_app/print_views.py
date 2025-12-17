from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.urls import reverse
from django.utils import timezone
from django.http import HttpResponse

from .models import Patient, Assessment, ConsentDocument, AuditLog
from .utils.request_context import get_current_request, get_client_ip, get_user_agent


# Lazy imports to avoid circular dependency
def _get_calendar_weeks_func():
    from .views import generate_calendar_weeks
    return generate_calendar_weeks


def _get_completion_date_func():
    from .views import get_completion_date
    return get_completion_date


def _get_log_audit_action_func():
    from .views import log_audit_action
    return log_audit_action


def _get_build_url_func():
    from .views import build_url
    return build_url


@login_required
def print_clinical_path(request, patient_id: int):
    patient = get_object_or_404(Patient, id=patient_id)
    generate_calendar_weeks = _get_calendar_weeks_func()
    calendar_weeks, assessment_events = generate_calendar_weeks(patient)
    return_to = request.GET.get("return_to") or request.META.get("HTTP_REFERER")
    back_url = return_to or reverse("rtms_app:patient_clinical_path", args=[patient.id])
    return render(request, "rtms_app/print/path.html", {
        "patient": patient,
        "calendar_weeks": calendar_weeks,
        "assessment_events": assessment_events,
        "back_url": back_url,
    })


@login_required
def patient_print_discharge(request, patient_id):
    patient = get_object_or_404(Patient, id=patient_id)
    build_url = _get_build_url_func()
    return_to = request.GET.get("return_to") or request.META.get("HTTP_REFERER")
    return redirect(
        build_url(
            'rtms_app_print:patient_print_bundle',
            args=[patient.id],
            query={'docs': ['discharge'], 'return_to': return_to} if return_to else {'docs': ['discharge']},
        )
    )


@login_required
def patient_print_referral(request, patient_id):
    patient = get_object_or_404(Patient, id=patient_id)
    build_url = _get_build_url_func()
    return_to = request.GET.get("return_to") or request.META.get("HTTP_REFERER")
    return redirect(
        build_url(
            'rtms_app_print:patient_print_bundle',
            args=[patient.id],
            query={'docs': ['referral'], 'return_to': return_to} if return_to else {'docs': ['referral']},
        )
    )


@login_required
def patient_print_bundle(request, patient_id):
    patient = get_object_or_404(Patient, id=patient_id)
    get_completion_date = _get_completion_date_func()
    log_audit_action = _get_log_audit_action_func()

    return_to = request.GET.get("return_to") or request.META.get("HTTP_REFERER")

    raw_docs = request.GET.getlist("docs")
    if not raw_docs:
        legacy_docs = request.GET.get("docs")
        if legacy_docs:
            raw_docs = legacy_docs.split(",")

    legacy_map = {
        "consent": "consent_pdf",
    }
    raw_docs = [legacy_map.get(doc, doc) for doc in raw_docs]

    DOC_DEFINITIONS = {
        "admission": {
            "label": "初診時サマリー",
            "template": "rtms_app/print/admission_summary.html",
        },
        "suitability": {
            "label": "rTMS問診票",
            "template": "rtms_app/print/suitability_questionnaire.html",
        },
        "consent_pdf": {
            "label": "説明同意書（PDF）",
            "pdf_static": "rtms_app/docs/rtms_consent_latest.pdf",
        },
        "discharge": {
            "label": "退院時サマリー",
            "template": "rtms_app/print/discharge_summary.html",
        },
        "referral": {
            "label": "紹介状",
            "template": "rtms_app/print/referral.html",
        },
    }
    DOC_ORDER = ["admission", "suitability", "consent_pdf", "discharge", "referral"]

    selected_doc_keys = [d for d in DOC_ORDER if d in raw_docs]

    assessments = Assessment.objects.filter(
        patient=patient
    ).order_by("date")

    end_date_est = get_completion_date(patient.first_treatment_date)
    today = timezone.now().date()
    back_url = return_to or reverse("rtms_app:patient_first_visit", args=[patient.id])

    docs_to_render = []
    for key in selected_doc_keys:
        if key not in DOC_DEFINITIONS:
            continue
        doc_info = DOC_DEFINITIONS[key].copy()
        doc_info["key"] = key
        docs_to_render.append(doc_info)

    context = {
        "patient": patient,
        "docs_to_render": docs_to_render,
        "doc_definitions": DOC_DEFINITIONS,
        "selected_doc_keys": selected_doc_keys,
        "assessments": assessments,
        "test_scores": assessments,
        "consent_copies": ["患者控え", "病院控え"],
        "end_date_est": end_date_est,
        "today": today,
        "back_url": back_url,
    }

    # 印刷ログ記録
    for doc_key in selected_doc_keys:
        doc_label = DOC_DEFINITIONS.get(doc_key, {}).get('label', doc_key)
        meta = {
            'docs': selected_doc_keys,
            'querystring': request.GET.urlencode(),
            'return_to': return_to,
        }
        log_audit_action(patient, 'PRINT', 'Document', doc_key, f'{doc_label}印刷', meta)

    return render(
        request,
        "rtms_app/print/bundle.html",
        context,
    )
