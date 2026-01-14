from django.urls import path
from . import print_views

app_name = "print"

urlpatterns = [
    path("bundle/", print_views.patient_print_bundle, name="patient_print_bundle"),
    path("bundle/pdf/", print_views.patient_print_bundle_pdf, name="patient_print_bundle_pdf"),
    path("path/", print_views.print_clinical_path, name="print_clinical_path"),
    path("path/pdf/", print_views.print_clinical_path_pdf, name="print_clinical_path_pdf"),
    path("admission/", print_views.patient_print_admission, name="patient_print_admission"),
    path("admission/pdf/", print_views.patient_print_admission_pdf, name="patient_print_admission_pdf"),
    path("discharge/", print_views.patient_print_discharge, name="patient_print_discharge"),
    path("discharge/pdf/", print_views.patient_print_discharge_pdf, name="patient_print_discharge_pdf"),
    path("referral/", print_views.patient_print_referral, name="patient_print_referral"),
    path("referral/pdf/", print_views.patient_print_referral_pdf, name="patient_print_referral_pdf"),
    path("suitability/", print_views.patient_print_suitability, name="patient_print_suitability"),
    path("suitability/pdf/", print_views.patient_print_suitability_pdf, name="patient_print_suitability_pdf"),
    path("side_effect/<int:session_id>/", print_views.print_side_effect_check, name="print_side_effect_check"),
    path("side_effect/<int:session_id>/pdf/", print_views.print_side_effect_check_pdf, name="print_side_effect_check_pdf"),
    path("treatment/record/<int:session_id>/", print_views.print_side_effect_check, name="print_treatment_record_preview"),
]