from django.urls import path
from . import views

urlpatterns = [
    # --- 1. トップ画面 (業務ダッシュボード) ---
    path('dashboard/', views.dashboard_view, name='dashboard'),

    # --- 2. 患者登録 (簡易) ---
    # トップページの「新規登録」ボタンから遷移
    path('patient/add/', views.patient_add_view, name='patient_add'),

    # --- 3. 初診・基本情報・適正質問票 ---
    # ダッシュボードの「初診」「入院」タブの患者クリック時
    path('patient/<int:patient_id>/first_visit/', views.patient_first_visit, name='patient_first_visit'),

    # --- 4. 位置決め記録 ---
    # ダッシュボードの「位置決め」タブの患者クリック時
    path('patient/<int:patient_id>/mapping/add/', views.mapping_add, name='mapping_add'),

    # --- 5. 治療実施 (副作用チェック含む) ---
    # ダッシュボードの「治療実施」タブの患者クリック時
    path('patient/<int:patient_id>/treatment/add/', views.treatment_add, name='treatment_add'),

    # --- 6. 状態評価 (HAM-D等) ---
    # ダッシュボードの「状態評価」タブの患者クリック時
    path('patient/<int:patient_id>/assessment/add/', views.assessment_add, name='assessment_add'),

    # --- 7. 管理者機能 (データ出力) ---
    path('export/csv/', views.export_treatment_csv, name='export_csv'),
    path('backup/db/', views.download_db, name='download_db'),
    
    # ... 8. サマリー（データ出力）
    path('patient/<int:patient_id>/summary/', views.patient_summary_view, name='patient_summary'),
    
    path('patient/list/', views.patient_list_view, name='patient_list'),
    path('patient/<int:patient_id>/admission/', views.admission_procedure, name='admission_procedure'),
]