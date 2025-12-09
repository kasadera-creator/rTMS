from django.urls import path
from . import views

urlpatterns = [
    path('dashboard/', views.dashboard_view, name='dashboard'),
    path('patient/add/', views.patient_add_view, name='patient_add'),
    path('export/csv/', views.export_treatment_csv, name='export_csv'),
    path('backup/db/', views.download_db, name='download_db'),
]