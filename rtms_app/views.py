from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.utils.dateparse import parse_date
from datetime import timedelta
from django.http import HttpResponse, FileResponse
from django.conf import settings
import os
import csv

from .models import Patient, TreatmentSession
from .forms import PatientBasicForm, PatientScheduleForm, PatientRegistrationForm

# --- ダッシュボード (日付移動機能付き) ---
@login_required
def dashboard_view(request):
    """
    トップ画面：業務タスク一覧 (日付移動対応)
    """
    # 1. 表示する日付の決定
    date_str = request.GET.get('date')
    if date_str:
        target_date = parse_date(date_str) or timezone.now().date()
    else:
        target_date = timezone.now().date()
    
    # 前日と翌日（ナビゲーション用）
    prev_day = target_date - timedelta(days=1)
    next_day = target_date + timedelta(days=1)

    # 2. データの取得 (全て target_date を基準にする)

    # (A) 今日の初診 (登録日が target_date)
    new_patients_query = Patient.objects.filter(created_at__date=target_date)
    new_patients = []
    for p in new_patients_query:
        new_patients.append({'obj': p, 'status': 'done'})

    # (B) 今日の入院 (入院予定日が target_date)
    admissions_query = Patient.objects.filter(admission_date=target_date)
    admissions = []
    for p in admissions_query:
        admissions.append({'obj': p, 'status': 'todo'})

    # (C) 今日の位置決め (位置決め日が target_date)
    mappings_query = Patient.objects.filter(mapping_date=target_date)
    mappings = []
    for p in mappings_query:
        # その日に治療記録(MT測定など)があるかチェック
        has_session = TreatmentSession.objects.filter(patient=p, date__date=target_date).exists()
        mappings.append({
            'obj': p, 
            'status': 'done' if has_session else 'todo'
        })

    # (D) 今日の治療 (初回治療日が target_date 以前の患者)
    active_patients = Patient.objects.filter(first_treatment_date__lte=target_date).order_by('card_id')
    treatments = []
    
    for p in active_patients:
        # その日の治療記録が存在するか確認
        sessions = TreatmentSession.objects.filter(patient=p, date__date=target_date)
        is_done = sessions.exists()
        
        # 過去を含めた全セッション数（通算回数）
        # ※未来の日付を見ている場合などは厳密には計算が変わりますが、簡易的に「現在のDB上の総数」を使います
        total_sessions = TreatmentSession.objects.filter(patient=p).count()
        next_session_no = total_sessions + 1 if not is_done else total_sessions
        
        # 評価日判定 (5回ごと)
        note = f"{next_session_no}回目"
        is_evaluation_day = False
        check_num = total_sessions if is_done else next_session_no
        
        if check_num > 0 and check_num % 5 == 0:
            note += " ★評価日"
            is_evaluation_day = True

        treatments.append({
            'obj': p,
            'status': 'done' if is_done else 'todo',
            'note': note,
            'is_eval': is_evaluation_day
        })

    context = {
        'today': target_date,
        'prev_day': prev_day,
        'next_day': next_day,
        'new_patients': new_patients,
        'admissions': admissions,
        'mappings': mappings,
        'treatments': treatments,
    }
    return render(request, 'rtms_app/dashboard.html', context)


# --- 患者詳細ページ ---
@login_required
def patient_detail_view(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    # 治療履歴
    sessions = TreatmentSession.objects.filter(patient=patient).order_by('-date')
    
    return render(request, 'rtms_app/patient_detail.html', {
        'patient': patient,
        'sessions': sessions
    })


# --- 基本情報編集 ---
@login_required
def patient_edit_basic(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    if request.method == 'POST':
        form = PatientBasicForm(request.POST, instance=patient)
        if form.is_valid():
            form.save()
            return redirect('patient_detail', patient_id=patient.id)
    else:
        form = PatientBasicForm(instance=patient)
    
    return render(request, 'rtms_app/patient_form.html', {
        'form': form, 'title': '基本情報の編集', 'patient': patient
    })


# --- スケジュール編集 ---
@login_required
def patient_edit_schedule(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    if request.method == 'POST':
        form = PatientScheduleForm(request.POST, instance=patient)
        if form.is_valid():
            form.save()
            return redirect('patient_detail', patient_id=patient.id)
    else:
        form = PatientScheduleForm(instance=patient)
    
    return render(request, 'rtms_app/patient_form.html', {
        'form': form, 'title': 'スケジュールの管理', 'patient': patient
    })


# --- 新規患者登録 (現場用) ---
@login_required
def patient_add_view(request):
    if request.method == 'POST':
        form = PatientRegistrationForm(request.POST)
        if form.is_valid():
            patient = form.save()
            return redirect('dashboard')
    else:
        form = PatientRegistrationForm()
    
    return render(request, 'rtms_app/patient_add.html', {'form': form})


# --- データ出力系 ---
@login_required
def export_treatment_csv(request):
    """治療記録をCSVで出力"""
    response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
    response['Content-Disposition'] = 'attachment; filename="treatment_data.csv"'

    writer = csv.writer(response)
    writer.writerow(['ID', 'Patient Name', 'Date', 'MT(%)', 'Intensity(%)', 'Pulses', 'Safety(Sleep)', 'Adverse Events'])

    treatments = TreatmentSession.objects.all().select_related('patient').order_by('date')
    for t in treatments:
        adverse_str = str(t.adverse_events) if t.adverse_events else ""
        writer.writerow([
            t.patient.card_id,
            t.patient.name,
            t.date.strftime('%Y-%m-%d %H:%M'),
            t.motor_threshold,
            t.intensity,
            t.total_pulses,
            'OK' if t.safety_sleep else 'NG',
            adverse_str
        ])
    return response

@login_required
def download_db(request):
    """DBバックアップ"""
    if not request.user.is_superuser:
        return HttpResponse("管理者のみ実行可能です", status=403)
        
    db_path = settings.DATABASES['default']['NAME']
    if os.path.exists(db_path):
        return FileResponse(open(db_path, 'rb'), as_attachment=True, filename='db.sqlite3')
    else:
        return HttpResponse("DBファイルが見つかりません", status=404)