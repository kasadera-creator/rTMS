from django.urls import path
from . import views

app_name = "rtms_app"

urlpatterns = [
    path("dashboard/", views.dashboard_view, name="dashboard"),

    # patients
    path("patients/", views.patient_list_view, name="patient_list"),
    path("patients/add/", views.patient_add_view, name="patient_add"),

    # patient hub (HOME)
    path("patient/<int:patient_id>/", views.patient_summary_view, name="patient_home"),

    # entry
    path("patient/<int:patient_id>/first-visit/", views.patient_first_visit, name="patient_first_visit"),
    path("patient/<int:patient_id>/admission/", views.admission_procedure, name="admission_procedure"),
    path("patient/<int:patient_id>/mapping/add/", views.mapping_add, name="mapping_add"),
    path("patient/<int:patient_id>/treatment/add/", views.treatment_add, name="treatment_add"),
    path("patient/<int:patient_id>/assessment/add/", views.assessment_add, name="assessment_add"),

    # path
    path("patient/<int:patient_id>/path/", views.patient_clinical_path, name="patient_clinical_path"),
    path(
        "patient/<int:patient_id>/path/print/",
        views.patient_print_path,
        name="print_clinical_path",
    ),

    # print
    path("patient/<int:patient_id>/print/bundle/", views.patient_print_bundle, name="patient_print_bundle"),
    path("patient/<int:patient_id>/print/discharge/", views.patient_print_discharge, name="patient_print_discharge"),
    path("patient/<int:patient_id>/print/referral/", views.patient_print_referral, name="patient_print_referral"),
    
    path("logout/", views.custom_logout, name="custom_logout"),

]
