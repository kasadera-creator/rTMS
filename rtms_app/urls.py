from django.urls import path
from . import views

urlpatterns = [
    path('dashboard/', views.dashboard_view, name='dashboard'),
    path('patient/list/', views.patient_list_view, name='patient_list'),
    path('patient/add/', views.patient_add_view, name='patient_add'),
    path('patient/<int:patient_id>/first_visit/', views.patient_first_visit, name='patient_first_visit'),
    path('patient/<int:patient_id>/admission/', views.admission_procedure, name='admission_procedure'),
    path('patient/<int:patient_id>/mapping/add/', views.mapping_add, name='mapping_add'),
    path('patient/<int:patient_id>/treatment/add/', views.treatment_add, name='treatment_add'),
    path('patient/<int:patient_id>/assessment/add/', views.assessment_add, name='assessment_add'),
    path('patient/<int:patient_id>/summary/', views.patient_summary_view, name='patient_summary'),
    path('export/csv/', views.export_treatment_csv, name='export_csv'),
    path('backup/db/', views.download_db, name='download_db'),
]