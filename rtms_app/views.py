from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.utils.dateparse import parse_date
from datetime import timedelta, date
from django.http import HttpResponse, FileResponse
from django.conf import settings
import os
import csv
import json

from .models import Patient, TreatmentSession, MappingSession, Assessment
from .forms import PatientFirstVisitForm, MappingForm, TreatmentForm, PatientScheduleForm

# ------------------------------------------------------------------
# ユーティリティ: 平日計算ロジック
# ------------------------------------------------------------------
def get_session_number(start_date, target_date):
    """
    初回治療日(start_date)からtarget_dateまで、
    土日を除いて何回目の治療か（何日目か）を計算する。
    ※祝日や年末年始の判定は簡易的に手動調整が必要な場合がありますが、
      ここでは土日除外ロジックで実装します。
    戻り値:
        1以上の整数: 第N回目
        0: 治療開始前
        -1: 土日など治療日ではない
    """
    if not start_date or target_date < start_date:
        return 0
    
    # 土日なら治療日ではない (-1)
    if target_date.weekday() >= 5: # 5=Sat, 6=Sun
        return -1

    current_date = start_date
    session_count = 0
    
    # 開始日からターゲット日までループして平日をカウント
    # (日数が多いと重くなるため、運用が長期間に及ぶ場合は数式計算に切り替え推奨)
    while current_date <= target_date:
        if current_date.weekday() < 5: # 平日のみカウント
            session_count += 1
        current_date += timedelta(days=1)
        
    return session_count

def is_holiday_or_weekend(d):
    """土日判定"""
    return d.weekday() >= 5

# ------------------------------------------------------------------
# 1. 業務ダッシュボード (トップ画面)
# ------------------------------------------------------------------
@login_required
def dashboard_view(request):
    """
    業務タスク一覧を表示するトップ画面
    """
    # 日付指定がなければ今日にリダイレクト
    if 'date' not in request.GET:
        jst_now = timezone.localtime(timezone.now())
        today_str = jst_now.strftime('%Y-%m-%d')
        return redirect(f'{request.path}?date={today_str}')

    date_str = request.GET.get('date')
    try:
        target_date = parse_date(date_str)
    except:
        target_date = timezone.now().date()

    if not target_date:
        target_date = timezone.now().date()

    # ナビゲーション用
    prev_day = target_date - timedelta(days=1)
    next_day = target_date + timedelta(days=1)

    # ----------------------------------------------
    # A. 今日の初診 (登録日 = target_date)
    # ----------------------------------------------
    new_patients_query = Patient.objects.filter(created_at__date=target_date)
    new_patients = []
    for p in new_patients_query:
        # 初診情報の入力チェック（必須項目が埋まっているかなど）
        # 簡易的に「患者データが存在すれば入力済み」とみなしますが、
        # 必要なら p.life_history 等の中身を確認して分岐します。
        status_label = "実施済"
        status_color = "success"
        
        new_patients.append({
            'obj': p, 
            'status': status_label,
            'color': status_color
        })

    # ----------------------------------------------
    # B. 今日の入院 (入院予定日 = target_date)
    # ----------------------------------------------
    admissions_query = Patient.objects.filter(admission_date=target_date)
    admissions = []
    for p in admissions_query:
        # 入院オリエンテーション等の完了フラグがないため、ひとまず表示のみ
        # 運用に合わせて「入院時記録」モデルを作れば判定可能
        admissions.append({
            'obj': p, 
            'status': "要対応",
            'color': "warning"
        })

    # ----------------------------------------------
    # C. 今日の位置決め (位置決め予定日 = target_date)
    # ----------------------------------------------
    mappings_scheduled = Patient.objects.filter(mapping_date=target_date)
    mappings = []
    for p in mappings_scheduled:
        # 実施済みチェック
        is_done = MappingSession.objects.filter(patient=p, date=target_date).exists()
        mappings.append({
            'obj': p, 
            'status': "実施済" if is_done else "実施未",
            'color': "success" if is_done else "danger"
        })

    # ----------------------------------------------
    # D. 今日の治療実施 (治療期間中の患者)
    # ----------------------------------------------
    # 「初回治療日が設定されている」患者すべてを対象に計算
    # (終了フラグがないため、全員分計算して「30回以内」の人だけ表示する)
    active_candidates = Patient.objects.filter(first_treatment_date__isnull=False).order_by('card_id')
    treatments = []
    assessments_due = [] # 評価対象者もここで探す
    
    for p in active_candidates:
        # 今日は何回目か計算
        session_num = get_session_number(p.first_treatment_date, target_date)
        
        # 治療対象外の日（土日）または まだ開始前、あるいは30回を大幅に超えている(例えば40回以上)なら表示しない
        if session_num <= 0 or session_num > 40:
            continue
            
        # 今日の実施記録があるか
        today_session = TreatmentSession.objects.filter(patient=p, date__date=target_date).first()
        is_done = today_session is not None
        
        # 表示用データ作成
        treatments.append({
            'obj': p,
            'note': f"第{session_num}回",
            'status': "実施済" if is_done else "実施未",
            'color': "success" if is_done else "danger", # 未実施は赤
            'session_num': session_num
        })

        # --- E. 状態評価の判定 (15回目と30回目) ---
        # 「今日が15回目」または「今日が30回目」の日であれば評価リストに入れる
        if session_num == 15 or session_num == 30:
            # 評価済みかチェック
            is_assessed = Assessment.objects.filter(patient=p, date=target_date).exists()
            
            reason_text = "中間評価 (15回目)" if session_num == 15 else "終了時評価 (30回目)"
            
            assessments_due.append({
                'obj': p,
                'reason': reason_text,
                'status': "実施済" if is_assessed else "実施未",
                'color': "success" if is_assessed else "danger"
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
# 2. 初診・基本情報入力
# ------------------------------------------------------------------
@login_required
def patient_first_visit(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    if request.method == 'POST':
        form = PatientFirstVisitForm(request.POST, instance=patient)
        if form.is_valid():
            form.save()
            return redirect('dashboard')
    else:
        form = PatientFirstVisitForm(instance=patient)
    return render(request, 'rtms_app/patient_first_visit.html', {'patient': patient, 'form': form})


# ------------------------------------------------------------------
# 3. 位置決め記録入力
# ------------------------------------------------------------------
@login_required
def mapping_add(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    history = MappingSession.objects.filter(patient=patient).order_by('date')
    
    if request.method == 'POST':
        form = MappingForm(request.POST)
        if form.is_valid():
            mapping = form.save(commit=False)
            mapping.patient = patient
            mapping.save()
            return redirect('dashboard')
    else:
        # 日付指定があればその日を初期値に
        initial_date = request.GET.get('date') or timezone.now().date()
        form = MappingForm(initial={'date': initial_date, 'week_number': 1})

    return render(request, 'rtms_app/mapping_add.html', {'patient': patient, 'form': form, 'history': history})


# ------------------------------------------------------------------
# 4. 治療実施入力
# ------------------------------------------------------------------
@login_required
def treatment_add(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    latest_mapping = MappingSession.objects.filter(patient=patient).order_by('-date').first()
    
    # 副作用項目定義
    side_effect_items = [
        ('headache', '頭痛'), ('scalp', '頭皮痛（刺激痛）'), ('discomfort', '刺激部位の不快感'),
        ('tooth', '歯痛'), ('twitch', '顔面のけいれん'), ('dizzy', 'めまい'),
        ('nausea', '吐き気'), ('tinnitus', '耳鳴り'), ('hearing', '聴力低下'),
        ('anxiety', '不安感・焦燥感'), ('other', 'その他'),
    ]

    if request.method == 'POST':
        form = TreatmentForm(request.POST)
        if form.is_valid():
            session = form.save(commit=False)
            session.patient = patient
            session.performer = request.user
            
            # 副作用データの収集
            se_data = {}
            for key, label in side_effect_items:
                val = request.POST.get(f'se_{key}') # 0,1,2,3
                if val: se_data[key] = val
            se_data['note'] = request.POST.get('se_note', '')
            
            session.side_effects = se_data
            session.save()
            return redirect('dashboard')
    else:
        # 日付指定があればその日時を初期値に
        target_date_str = request.GET.get('date')
        if target_date_str:
            # 時間は現在時刻、日付は指定日
            now = timezone.now()
            target = parse_date(target_date_str)
            initial_date = now.replace(year=target.year, month=target.month, day=target.day)
        else:
            initial_date = timezone.now()

        initial_data = {'date': initial_date, 'total_pulses': 1980, 'intensity': 120}
        if latest_mapping:
            initial_data['motor_threshold'] = latest_mapping.resting_mt
            
        form = TreatmentForm(initial=initial_data)

    return render(request, 'rtms_app/treatment_add.html', {
        'patient': patient,
        'form': form,
        'latest_mapping': latest_mapping,
        'side_effect_items': side_effect_items,
    })


# ------------------------------------------------------------------
# 5. 状態評価入力
# ------------------------------------------------------------------
@login_required
def assessment_add(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    history = Assessment.objects.filter(patient=patient).order_by('date')
    
    if request.method == 'POST':
        try:
            # 簡易保存ロジック (本来はFormクラス推奨)
            scores = {}
            # q1 ~ q21 の値を取得
            for i in range(1, 22):
                key = f'q{i}'
                scores[key] = request.POST.get(key, 0)

            assessment = Assessment(
                patient=patient,
                date=request.POST.get('date', timezone.now().date()),
                type='HAM-D',
                scores=scores,
                timing=request.POST.get('timing', 'other'),
                note=request.POST.get('note', '')
            )
            assessment.calculate_scores()
            assessment.save()
            return redirect('dashboard')
        except Exception as e:
            print(e)
            
    # 日付指定があれば初期値に
    initial_date = request.GET.get('date') or timezone.now().date()
            
    return render(request, 'rtms_app/assessment_add.html', {
        'patient': patient,
        'history': history,
        'today': initial_date
    })


# ------------------------------------------------------------------
# 6. 新規患者登録
# ------------------------------------------------------------------
@login_required
def patient_add_view(request):
    if request.method == 'POST':
        from .forms import PatientRegistrationForm
        form = PatientRegistrationForm(request.POST)
        if form.is_valid():
            form.save()
            return redirect('dashboard')
    else:
        from .forms import PatientRegistrationForm
        form = PatientRegistrationForm()
    return render(request, 'rtms_app/patient_add.html', {'form': form})


# ------------------------------------------------------------------
# 7. 管理者用機能
# ------------------------------------------------------------------
@login_required
def export_treatment_csv(request):
    response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
    response['Content-Disposition'] = 'attachment; filename="treatment_data.csv"'
    writer = csv.writer(response)
    writer.writerow(['ID', '氏名', '実施日時', 'MT(%)', '強度(%)', 'パルス数', '実施者', '副作用'])
    
    treatments = TreatmentSession.objects.all().select_related('patient', 'performer').order_by('date')
    for t in treatments:
        se_str = json.dumps(t.side_effects, ensure_ascii=False) if t.side_effects else ""
        writer.writerow([
            t.patient.card_id, t.patient.name, t.date.strftime('%Y-%m-%d %H:%M'),
            t.motor_threshold, t.intensity, t.total_pulses,
            t.performer.username if t.performer else "", se_str
        ])
    return response

@login_required
def download_db(request):
    if not request.user.is_staff: return HttpResponse("Forbidden", status=403)
    db_path = settings.DATABASES['default']['NAME']
    if os.path.exists(db_path):
        return FileResponse(open(db_path, 'rb'), as_attachment=True, filename='db.sqlite3')
    return HttpResponse("Not found", status=404)