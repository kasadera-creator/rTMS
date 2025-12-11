from django.contrib import admin
from django.urls import path, include
from django.views.generic import RedirectView
from rtms_app import views # app nameに合わせて調整してください

urlpatterns = [
    # トップページに来たらダッシュボードへ転送
    path('', RedirectView.as_view(url='/app/dashboard/', permanent=False)),
    
    # アプリのURLを '/app/' 以下に割り当て
    path('app/dashboard/', views.dashboard_view, name='dashboard'),
    path('app/patient/list/', views.patient_list_view, name='patient_list'),
    path('app/patient/add/', views.patient_add_view, name='patient_add'),
    path('app/patient/<int:patient_id>/first_visit/', views.patient_first_visit, name='patient_first_visit'),
    path('app/patient/<int:patient_id>/admission/', views.admission_procedure, name='admission_procedure'),
    path('app/patient/<int:patient_id>/mapping/add/', views.mapping_add, name='mapping_add'),
    path('app/patient/<int:patient_id>/treatment/add/', views.treatment_add, name='treatment_add'),
    path('app/patient/<int:patient_id>/assessment/add/', views.assessment_add, name='assessment_add'),
    path('app/patient/<int:patient_id>/summary/', views.patient_summary_view, name='patient_summary'),
    
    # 印刷・パス関連
    path('app/patient/<int:pk>/first_visit/print/', views.patient_print_preview, name='patient_print_preview'),
    path('app/patient/<int:pk>/summary/print/', views.patient_print_summary, name='patient_print_summary'),
    
    # ★新規追加: クリニカルパス
    path('app/patient/<int:patient_id>/clinical_path/', views.patient_clinical_path, name='patient_clinical_path'),

    path('app/export/csv/', views.export_treatment_csv, name='export_csv'),
    path('app/backup/db/', views.download_db, name='download_db'),
    path('app/logout/', views.custom_logout_view, name='custom_logout'),
    
    path('admin/', admin.site.urls),
]