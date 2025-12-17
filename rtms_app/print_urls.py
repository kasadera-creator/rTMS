from django.urls import path
from . import print_views

app_name = "rtms_app_print"

urlpatterns = [
    path("patient/<int:patient_id>/print/bundle/", print_views.patient_print_bundle, name="patient_print_bundle"),
    path("patient/<int:patient_id>/print/path/", print_views.print_clinical_path, name="print_clinical_path"),
    path("patient/<int:patient_id>/print/discharge/", print_views.patient_print_discharge, name="patient_print_discharge"),
    path("patient/<int:patient_id>/print/referral/", print_views.patient_print_referral, name="patient_print_referral"),
]
