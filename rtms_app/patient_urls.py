from django.urls import path

from . import views_patient

app_name = "patient_portal"

urlpatterns = [
    path("login/", views_patient.patient_login, name="login"),
    path("logout/", views_patient.patient_logout, name="logout"),
    path("", views_patient.portal, name="portal"),
    path("surveys/start/", views_patient.start_session, name="start"),
    path("surveys/<int:session_id>/review/", views_patient.review, name="review"),
    path("surveys/<int:session_id>/submit/", views_patient.submit, name="submit"),
    path("surveys/<int:session_id>/<str:instrument>/", views_patient.instrument_view, name="instrument"),
]
