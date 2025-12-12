# rtms_app/urls.py
from django.urls import path
from . import views

app_name = "rtms_app"

urlpatterns = [
    # dashboard
    path("dashboard/", views.dashboard_view, name="dashboard"),

    # patients
    path("patient/list/", views.patient_list_view, name="patient_list"),
    path("patient/add/", views.patient_add_view, name="patient_add"),

    # patient context（★二重スラッシュ廃止＋patient_id導入）
    path("patient/<int:patient_id>/first_visit/", views.patient_first_visit, name="patient_first_visit"),
    path("patient/<int:patient_id>/admission/", views.admission_procedure, name="admission_procedure"),
    path("patient/<int:patient_id>/mapping/add/", views.mapping_add, name="mapping_add"),
    path("patient/<int:patient_id>/treatment/add/", views.treatment_add, name="treatment_add"),
    path("patient/<int:patient_id>/assessment/add/", views.assessment_add, name="assessment_add"),

    # summary / clinical path
    path("patient/<int:patient_id>/summary/", views.patient_summary_view, name="patient_summary"),
    path("patient/<int:patient_id>/path/", views.patient_clinical_path, name="patient_clinical_path"),
    path("patient/<int:patient_id>/path/print/", views.patient_print_path, name="patient_print_path"),

    # print
    path("patient/<int:patient_id>/first_visit/print/", views.patient_print_preview, name="patient_print_preview"),
    path("patient/<int:patient_id>/summary/print/", views.patient_print_summary, name="patient_print_summary"),

    # maintenance / export
    path("export/csv/", views.export_treatment_csv, name="export_csv"),
    path("backup/db/", views.download_db, name="download_db"),
    path("logout/", views.custom_logout_view, name="custom_logout"),
]
