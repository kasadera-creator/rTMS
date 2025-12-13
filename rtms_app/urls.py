from django.urls import path
from . import views

urlpatterns = [
    # dashboard
    path("dashboard/", views.dashboard_view, name="dashboard"),

    # patients
    path("patient/list/", views.patient_list_view, name="patient_list"),
    path("patient/add/", views.patient_add_view, name="patient_add"),

    # 患者ホーム（＝患者サマリーを流用）
    path("patient/<int:patient_id>/", views.patient_summary_view, name="patient_home"),

    # 初診・入院・位置決め・治療・尺度
    path("patient/<int:patient_id>/first_visit/", views.patient_first_visit, name="patient_first_visit"),
    path("patient/<int:patient_id>/admission/", views.admission_procedure, name="admission_procedure"),
    path("patient/<int:patient_id>/mapping/add/", views.mapping_add, name="mapping_add"),
    path("patient/<int:patient_id>/treatment/add/", views.treatment_add, name="treatment_add"),
    path("patient/<int:patient_id>/assessment/add/", views.assessment_add, name="assessment_add"),
    path("patient/<int:patient_id>/print/bundle/", views.patient_print_bundle, name="patient_print_bundle"),

    # パス（表示・印刷）
    path("patient/<int:patient_id>/path/", views.patient_clinical_path, name="patient_clinical_path"),
    path("patient/<int:patient_id>/path/print/", views.patient_print_path, name="patient_print_path"),

    # 印刷（既存）
    path("patient/<int:patient_id>/first_visit/print/", views.patient_print_preview, name="patient_print_preview"),
    path("patient/<int:patient_id>/summary/print/", views.patient_print_summary, name="patient_print_summary"),

    # maintenance
    path("export/csv/", views.export_treatment_csv, name="export_csv"),
    path("backup/db/", views.download_db, name="download_db"),
    path("logout/", views.custom_logout_view, name="custom_logout"),
    

]
