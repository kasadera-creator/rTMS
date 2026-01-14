from django.urls import path, include
from django.shortcuts import redirect
from . import views
from django.views.generic.base import RedirectView
from . import views_health

from django.conf import settings
from django.conf.urls.static import static

app_name = "rtms_app"

urlpatterns = [
    # =========================
    # Health & System
    # =========================
    path("healthz/", views_health.healthz, name="healthz"),
    path("version/", views_health.version, name="version"),
    
    # =========================
    # Dashboard / List
    # =========================
    path("dashboard/", views.dashboard_view, name="dashboard"),
    path("patients/", views.patient_list_view, name="patient_list"),
    path("patients/add/", views.patient_add_view, name="patient_add"),
    path("logout/", views.custom_logout, name="custom_logout"),
    
    path("app/consent/latest/", views.latest_consent, name="latest_consent"),

    # =========================
    # Patient main pages
    # =========================

    # ★ 患者ホーム = 初診・基本情報
    path(
        "patient/<int:patient_id>/",
        views.patient_first_visit,
        name="patient_first_visit",
    ),

    # ★ 基本情報編集（権限制限）
    path(
        "patient/<int:patient_id>/basic/edit/",
        views.patient_basic_edit,
        name="patient_basic_edit",
    ),

    # ★ 退院準備（サマリー）
    path(
        "patient/<int:patient_id>/summary/",
        views.patient_summary_view,
        name="patient_home",
    ),

    # ★ 適正質問票（別画面）
    path(
        "patient/<int:patient_id>/questionnaire/",
        views.questionnaire_edit,
        name="questionnaire_edit",
    ),

    # ---- 後方互換（旧URL） ----
    path(
        "patient/<int:patient_id>/first-visit/",
        lambda request, patient_id: redirect(
            "rtms_app:patient_first_visit", patient_id=patient_id
        ),
    ),

    # =========================
    # Clinical workflow
    # =========================
    path(
        "patient/<int:patient_id>/admission/",
        views.admission_procedure,
        name="admission_procedure",
    ),
    path(
        "patient/<int:patient_id>/mapping/add/",
        views.mapping_add,
        name="mapping_add",
    ),
    path(
        "patient/<int:patient_id>/treatment/add/",
        views.treatment_add,
        name="treatment_add",
    ),
    path(
        "patient/<int:patient_id>/assessment/week4/",
        views.assessment_week4,
        name="assessment_week4",
    ),
    # Short URL compatibility: redirect /assessment/<timing>/ -> /assessment/<timing>/add/
    path(
        "patient/<int:patient_id>/assessment/<str:timing>/",
        RedirectView.as_view(pattern_name='rtms_app:assessment_add', query_string=True, permanent=False),
        name="assessment_shortcut",
    ),
    path(
        "patient/<int:patient_id>/assessment/<str:timing>/add/",
        views.assessment_add,
        name="assessment_add",
    ),
    path(
        "patient/<int:patient_id>/assessment/hub/<str:timing>/",
        views.assessment_hub,
        name="assessment_hub",
    ),
    path(
        "patient/<int:patient_id>/assessment/<str:timing>/<str:scale_code>/",
        views.assessment_scale_form,
        name="assessment_scale",
    ),
    # Note: assessment_hub removed due to compatibility issues; link directly to `assessment_add`.

    # =========================
    # Path / Calendar
    # =========================
    path(
        "patient/<int:patient_id>/path/",
        views.patient_clinical_path,
        name="patient_clinical_path",
    ),
    path(
        "calendar/month/",
        views.calendar_month_view,
        name="calendar_month",
    ),
    path(
        "calendar/month/print/",
        views.calendar_month_print_view,
        name="calendar_month_print",
    ),

    # =========================
    # Print（分離）
    # =========================
    path("patient/<int:patient_id>/print/", include(("rtms_app.print_urls", "print"), namespace="print")),

    path("consent/latest/", views.consent_latest, name="consent_latest"),

    path("patient/<int:patient_id>/audit_logs/", views.audit_logs_view, name="audit_logs"),

    # =========================
    # Adverse Event Reports
    # =========================
    path(
        "adverse-event/print-preview/",
        views.adverse_event_report_print_preview,
        name="adverse_event_report_print_preview",
    ),
    path(
        "adverse-event/<int:session_id>/print/",
        views.adverse_event_report_print,
        name="adverse_event_report_print",
    ),

]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)


