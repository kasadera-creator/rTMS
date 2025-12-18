from django.urls import path
from . import print_views

app_name = "print"

urlpatterns = [
    path("bundle/", print_views.patient_print_bundle, name="patient_print_bundle"),
    path("path/", print_views.print_clinical_path, name="print_clinical_path"),
    path("admission/", print_views.patient_print_admission, name="patient_print_admission"),
    path("discharge/", print_views.patient_print_discharge, name="patient_print_discharge"),
    path("referral/", print_views.patient_print_referral, name="patient_print_referral"),
    path("side_effect/<int:session_id>/", print_views.print_side_effect_check, name="print_side_effect_check"),
]