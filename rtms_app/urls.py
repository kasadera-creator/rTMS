from django.urls import path, include
from django.shortcuts import redirect
from . import views
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
        "patient/<int:patient_id>/mapping/upsert-from-wizard/",
        views.mapping_upsert_from_wizard,
        name="mapping_upsert_from_wizard",
    ),
    path(
        "patient/<int:patient_id>/treatment/add/",
        views.treatment_add,
        name="treatment_add",
    ),
    path(
        "patient/<int:patient_id>/mapping/upsert-from-wizard/",
        views.mapping_upsert_from_wizard,
        name="mapping_upsert_from_wizard",
    ),
    path(
        "patient/<int:patient_id>/assessment/week4/",
        views.assessment_week4,
        name="assessment_week4",
    ),
    path(
        "patient/<int:patient_id>/assessment/<str:timing>/add/",
        views.assessment_add,
        name="assessment_add",
    ),

    # ---- New assessment flow (hub + scale forms) ----
    path(
        "patient/<int:patient_id>/assessment/<str:timing>/",
        views.assessment_hub,
        name="assessment_hub",
    ),
    path(
        "patient/<int:patient_id>/assessment/<str:timing>/<str:scale_code>/",
        views.assessment_scale_form,
        name="assessment_scale",
    ),

    # =========================
    # Path / Calendar
    # =========================
    path(
        "patient/<int:patient_id>/path/",
        views.patient_clinical_path,
        name="patient_clinical_path",
    ),

    # =========================
    # Print（分離）
    # =========================
    path("patient/<int:patient_id>/print/", include(("rtms_app.print_urls", "print"), namespace="print")),

    path("consent/latest/", views.consent_latest, name="consent_latest"),

    path("patient/<int:patient_id>/audit_logs/", views.audit_logs_view, name="audit_logs"),

]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)


