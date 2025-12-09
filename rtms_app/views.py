from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.utils.dateparse import parse_date
from datetime import timedelta
from django.http import HttpResponse, FileResponse
from django.conf import settings
import os
import csv
import json

from .models import Patient, TreatmentSession, MappingSession, Assessment
from .forms import PatientFirstVisitForm, MappingForm, TreatmentForm, PatientScheduleForm

# ------------------------------------------------------------------
# 1. 業務ダッシュボード (トップ画面)
# ------------------------------------------------------------------
@login_required
def dashboard_view(request):
    """
    業務タスク一覧を表示するトップ画面
    - 日付指定がない場合は「今日」にリダイレクト
    - 4つの業務カテゴリ＋状態評価のToDoリストを表示
    """
    # 日付指定がない場合、今日の日付を付与してリロード (URLを固定するため)
    if 'date' not in request.GET:
        today_str = timezone.now().strftime('%Y-%m-%d')
        return redirect(f'{request.path}?date={today_str}')

    # 日付の取得
    date_str = request.GET.get('date')
    try:
        target_date = parse_date(date_str)
    except:
        target_date = timezone.now().date()
        
    if not target_date:
        target_date = timezone.now().date()

    # 前日・翌日ナビゲーション用
    prev_day = target_date - timedelta(days=1)
    next_day = target_date + timedelta(days=1)

    # --- A. 今日の初診 (登録日がターゲット日付) ---
    # ※運用上、登録日＝初診日と仮定します
    new_patients_query = Patient.objects.filter(created_at__date=target_date)
    new_patients = []
    for p in new_patients_query:
        new_patients.append({'obj': p, 'status': 'done'})

    # --- B. 今日の入院 (入院予定日がターゲット日付) ---
    admissions_query = Patient.objects.filter(admission_date=target_date)
    admissions = []
    for p in admissions_query:
        admissions.append({'obj': p, 'status': 'todo'})

    # --- C. 今日の位置決め (位置決め予定日がターゲット日付) ---
    mappings_scheduled = Patient.objects.filter(mapping_date=target_date)
    mappings = []
    for p in mappings_scheduled:
        # すでにその日の実施記録があるか確認
        is_done = MappingSession.objects.filter(patient=p, date=target_date).exists()
        mappings.append({
            'obj': p, 
            'status': 'done' if is_done else 'todo'
        })

    # --- D. 今日の治療実施 (初回治療日到来済み & 治療中の患者) ---
    # 簡易的に「初回治療日が今日以前」かつ「まだ完了フラグがない(運用上)」患者を取得
    # ここでは全患者からチェックする形をとります（人数が増えたら要最適化）
    active_candidates = Patient.objects.filter(first_treatment_date__lte=target_date).order_by('card_id')
    treatments = []
    
    for p in active_candidates:
        # 今日の治療記録があるか
        today_session = TreatmentSession.objects.filter(patient=p, date__date=target_date).first()
        is_done = today_session is not None
        
        # セッション回数計算 (今日実施済みならそれを含み、未実施なら次回)
        past_sessions = TreatmentSession.objects.filter(patient=p).count()
        current_session_no = past_sessions if is_done else (past_sessions + 1)
        
        # 30回(6週)を超えていたらリストから外す等のロジックも可ですが、一旦全員表示
        
        # 評価日アラート (5の倍数回)
        note = f"{current_session_no}回目"
        is_eval_day = (current_session_no > 0 and current_session_no % 5 == 0)
        
        treatments.append({
            'obj': p,
            'status': 'done' if is_done else 'todo',
            'note': note,
            'is_eval': is_eval_day
        })

    # --- E. 状態評価 (HAM-D等) ---
    # 治療予定リストの中から、評価時期の人を抽出
    assessments_due = []
    for item in treatments:
        if item['is_eval']: # 評価日のフラグが立っている人
            # すでに今日評価済みかチェック
            done_assessment = Assessment.objects.filter(patient=item['obj'], date=target_date).exists()
            assessments_due.append({
                'obj': item['obj'],
                'reason': item['note'], # "15回目" など
                'status': 'done' if done_assessment else 'todo'
            })

    context = {
        'today': target_date,
        'prev_day': prev_day,
        'next_day': next_day,
        'new_patients': new_patients,
        'admissions': admissions,
        'mappings': mappings,
        'treatments': treatments,
        'assessments_due': assessments_due,
    }
    return render(request, 'rtms_app/dashboard.html', context)


# ------------------------------------------------------------------
# 2. 初診・基本情報入力 (適正質問票含む)
# ------------------------------------------------------------------
@login_required
def patient_first_visit(request, patient_id):
    """
    初診・入院タブから遷移。
    基本情報、病歴、適正質問票を入力・編集・印刷する画面。
    """
    patient = get_object_or_404(Patient, pk=patient_id)
    
    if request.method == 'POST':
        form = PatientFirstVisitForm(request.POST, instance=patient)
        if form.is_valid():
            form.save()
            # 保存後は同じ画面に留まり、完了メッセージを出すなどが親切ですが
            # ここではダッシュボードに戻します
            return redirect('dashboard')
    else:
        form = PatientFirstVisitForm(instance=patient)
    
    return render(request, 'rtms_app/patient_first_visit.html', {
        'patient': patient, 
        'form': form
    })


# ------------------------------------------------------------------
# 3. 位置決め記録入力
# ------------------------------------------------------------------
@login_required
def mapping_add(request, patient_id):
    """
    位置決めタブから遷移。
    週ごとの位置決め情報を入力する画面。
    """
    patient = get_object_or_404(Patient, pk=patient_id)
    
    # 過去の履歴取得
    history = MappingSession.objects.filter(patient=patient).order_by('date')
    
    if request.method == 'POST':
        form = MappingForm(request.POST)
        if form.is_valid():
            mapping = form.save(commit=False)
            mapping.patient = patient
            mapping.save()
            return redirect('dashboard')
    else:
        # 初期値: 今日、第1週など
        form = MappingForm(initial={'date': timezone.now().date(), 'week_number': 1})

    return render(request, 'rtms_app/mapping_add.html', {
        'patient': patient,
        'form': form,
        'history': history
    })


# ------------------------------------------------------------------
# 4. 治療実施入力 (副作用チェック含む)
# ------------------------------------------------------------------
@login_required
def treatment_add(request, patient_id):
    """
    治療実施タブから遷移。
    日々の記録、安全確認、副作用チェックを入力・印刷する画面。
    """
    patient = get_object_or_404(Patient, pk=patient_id)
    
    # 最新の位置決め情報を取得 (MT表示用)
    latest_mapping = MappingSession.objects.filter(patient=patient).order_by('-date').first()
    
    # 今日の日付で既にデータがあればそれを編集モードで開く（オプション）
    # ここではシンプルに「常に新規作成」とします（1日2セッションの場合もあるため）
    
    if request.method == 'POST':
        form = TreatmentForm(request.POST)
        if form.is_valid():
            session = form.save(commit=False)
            session.patient = patient
            session.performer = request.user
            
            # 副作用チェックデータの処理 (HTML側のname="side_effect_key"等を拾う)
            # ここでは簡易的に、POSTデータから副作用関連のキーを抽出してJSON化する例です
            # 実際には django-jsonform を使うと自動で clean されますが
            # 手動で拾う場合は以下のようなロジックが必要です
            side_effect_data = {}
            # checkbox等の値を取得（実装に合わせて調整してください）
            # 今回はモデル側でJSONFieldを定義しているので、フォーム側でうまく処理されている前提
            # もしフォームにJSONフィールドがない場合、ここで構築します
            
            session.save()
            return redirect('dashboard')
    else:
        # 初期値セット
        initial_data = {
            'date': timezone.now(),
            'total_pulses': 1980, # Brainsway標準
            'intensity': 120,
        }
        if latest_mapping:
            initial_data['motor_threshold'] = latest_mapping.resting_mt
            
        form = TreatmentForm(initial=initial_data)

    return render(request, 'rtms_app/treatment_add.html', {
        'patient': patient,
        'form': form,
        'latest_mapping': latest_mapping
    })


# ------------------------------------------------------------------
# 5. 状態評価入力 (HAM-D)
# ------------------------------------------------------------------
@login_required
def assessment_add(request, patient_id):
    """
    状態評価タブから遷移。
    HAM-Dなどのスコアを入力する画面。
    """
    patient = get_object_or_404(Patient, pk=patient_id)
    
    # 過去の評価履歴
    history = Assessment.objects.filter(patient=patient).order_by('date')
    
    if request.method == 'POST':
        # ここはフォームクラスを作っていないため、手動で受ける簡易実装例
        # 本格的には AssessmentForm を forms.py に作るべきです
        try:
            score = int(request.POST.get('total_score_21', 0))
            assessment = Assessment.objects.create(
                patient=patient,
                date=request.POST.get('date', timezone.now().date()),
                type='HAM-D',
                total_score_21=score,
                timing=request.POST.get('timing', 'other'),
                note=request.POST.get('note', '')
            )
            assessment.calculate_scores() # 17項目の再計算など
            assessment.save()
            return redirect('dashboard')
        except ValueError:
            pass # エラー処理
            
    return render(request, 'rtms_app/assessment_add.html', {
        'patient': patient,
        'history': history,
        'today': timezone.now().date()
    })


# ------------------------------------------------------------------
# 6. 新規患者登録 (現場用・簡易版)
# ------------------------------------------------------------------
@login_required
def patient_add_view(request):
    """
    ダッシュボードから新規患者を登録する画面
    """
    if request.method == 'POST':
        # PatientRegistrationForm は forms.py に定義されている前提
        # (前回の forms.py コードに含まれています)
        from .forms import PatientRegistrationForm 
        form = PatientRegistrationForm(request.POST)
        if form.is_valid():
            patient = form.save()
            return redirect('dashboard')
    else:
        from .forms import PatientRegistrationForm
        form = PatientRegistrationForm()
    
    return render(request, 'rtms_app/patient_add.html', {'form': form})


# ------------------------------------------------------------------
# 7. 管理者用機能 (CSV出力・DBバックアップ)
# ------------------------------------------------------------------
@login_required
def export_treatment_csv(request):
    """治療記録をCSVで出力"""
    response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
    response['Content-Disposition'] = 'attachment; filename="treatment_data.csv"'

    writer = csv.writer(response)
    # ヘッダー (Excelの副作用シートに合わせて拡張可能)
    writer.writerow(['ID', '氏名', '実施日時', 'MT(%)', '強度(%)', 'パルス数', '実施者', '副作用有無'])

    treatments = TreatmentSession.objects.all().select_related('patient', 'performer').order_by('date')
    for t in treatments:
        # 副作用データの簡易整形
        side_effect_str = "なし"
        if t.side_effects:
             side_effect_str = json.dumps(t.side_effects, ensure_ascii=False)

        writer.writerow([
            t.patient.card_id,
            t.patient.name,
            t.date.strftime('%Y-%m-%d %H:%M'),
            t.motor_threshold,
            t.intensity,
            t.total_pulses,
            t.performer.username if t.performer else "不明",
            side_effect_str
        ])
    return response

@login_required
def download_db(request):
    """SQLite DBファイルのダウンロード (バックアップ用)"""
    if not request.user.is_staff: # スタッフ権限以上
        return HttpResponse("権限がありません", status=403)
        
    db_path = settings.DATABASES['default']['NAME']
    if os.path.exists(db_path):
        return FileResponse(open(db_path, 'rb'), as_attachment=True, filename='db.sqlite3')
    else:
        return HttpResponse("DBファイルが見つかりません", status=404)