from django.urls import path
from . import views

urlpatterns = [
    path('dashboard/', views.dashboard_view, name='dashboard'),
    path('patient/add/', views.patient_add_view, name='patient_add'),
    path('patient/<int:patient_id>/', views.patient_detail_view, name='patient_detail'),
    path('patient/<int:patient_id>/edit/basic/', views.patient_edit_basic, name='patient_edit_basic'),
    path('patient/<int:patient_id>/edit/schedule/', views.patient_edit_schedule, name='patient_edit_schedule'),
    path('export/csv/', views.export_treatment_csv, name='export_csv'),
    path('backup/db/', views.download_db, name='download_db'),
]