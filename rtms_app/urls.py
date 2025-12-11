from django.contrib import admin
from django.urls import path, include
from django.views.generic import RedirectView
from . import views

urlpatterns = [
    # トップページに来たらダッシュボードへ転送
    path('', RedirectView.as_view(url='/app/dashboard/', permanent=False)),
    
    path('dashboard/', views.dashboard_view, name='dashboard'),
    path('patient/list/', views.patient_list_view, name='patient_list'),
    path('patient/add/', views.patient_add_view, name='patient_add'),
    path('patient/<int:patient_id>/first_visit/', views.patient_first_visit, name='patient_first_visit'),
    path('patient/<int:patient_id>/admission/', views.admission_procedure, name='admission_procedure'),
    path('patient/<int:patient_id>/mapping/add/', views.mapping_add, name='mapping_add'),
    path('patient/<int:patient_id>/treatment/add/', views.treatment_add, name='treatment_add'),
    path('patient/<int:patient_id>/assessment/add/', views.assessment_add, name='assessment_add'),
    path('patient/<int:patient_id>/summary/', views.patient_summary_view, name='patient_summary'),
    
    # クリニカルパス (ブラウザ表示)
    path('patient/<int:patient_id>/path/', views.patient_clinical_path, name='patient_clinical_path'),
    # ★新規追加: クリニカルパス (印刷用)
    path('patient/<int:patient_id>/path/print/', views.patient_print_path, name='patient_print_path'),

    path('patient/<int:pk>/first_visit/print/', views.patient_print_preview, name='patient_print_preview'),
    path('patient/<int:pk>/summary/print/', views.patient_print_summary, name='patient_print_summary'),
    path('export/csv/', views.export_treatment_csv, name='export_csv'),
    path('backup/db/', views.download_db, name='download_db'),
    path('logout/', views.custom_logout_view, name='custom_logout'),
    
    path('admin/', admin.site.urls),
]