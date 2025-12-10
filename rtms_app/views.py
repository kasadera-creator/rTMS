from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.utils.dateparse import parse_date
from datetime import timedelta
import datetime # ★ここを追加（エラーの修正）
from django.http import HttpResponse, FileResponse
from django.conf import settings
from django.contrib.auth import logout
import os
import csv
import json

from .models import Patient, TreatmentSession, MappingSession, Assessment
from .forms import (
    PatientFirstVisitForm, MappingForm, TreatmentForm, 
    PatientRegistrationForm, AdmissionProcedureForm
)

# 平日計算ロジック
def get_session_number(start_date, target_date):
    if not start_date or target_date < start_date: return 0
    if target_date.weekday() >= 5: return -1
    current = start_date
    count = 0
    while current <= target_date:
        if current.weekday() < 5: count += 1
        current += timedelta(days=1)
    return count

# 30回目（終了予定日）の計算
def get_completion_date(start_date):
    if not start_date: return None
    current = start_date
    count = 0
    while count < 30:
        if current.weekday() < 5: count += 1
        if count == 30: return current
        current += timedelta(days=1)
    return current

@login_required
def dashboard_view(request):
    jst_now = timezone.localtime(timezone.now())
    if 'date' not in request.GET:
        return redirect(f'{request.path}?date={jst_now.strftime("%Y-%m-%d")}')
    try: target_date = parse_date(request.GET.get('date'))
    except: target_date = jst_now.date()
    if not target_date: target_date = jst_now.date()

    prev_day = target_date - timedelta(days=1)
    next_day = target_date + timedelta(days=1)

    new_patients = [{'obj': p, 'status': "登録済"} for p in Patient.objects.filter(created_at__date=target_date)]

    admissions = []
    for p in Patient.objects.filter(admission_date=target_date):
        status = "手続済" if p.is_admission_procedure_done else "要手続"
        color = "success" if p.is_admission_procedure_done else "warning"
        admissions.append({'obj': p, 'status': status, 'color': color})

    mappings = []
    for p in Patient.objects.filter(mapping_date=target_date):
        is_done = MappingSession.objects.filter(patient=p, date=target_date).exists()
        mappings.append({'obj': p, 'status': "実施済" if is_done else "実施未", 'color': "success" if is_done else "danger"})

    treatments = []
    assessments_due = []
    
    for adm in admissions:
        is_done = Assessment.objects.filter(patient=adm['obj'], date=target_date).exists()
        assessments_due.append({'obj': adm['obj'], 'reason': "入院時評価 (治療前)", 'status': "実施済" if is_done else "実施未", 'color': "success" if is_done else "danger", 'timing_code': 'baseline'})

    active_candidates = Patient.objects.filter(first_treatment_date__isnull=False).order_by('card_id')
    for p in active_candidates:
        session_num = get_session_number(p.first_treatment_date, target_date)
        
        # 30回目は「治療」タブには出さず、「状態評価・退院準備」タブに出す
        if session_num == 30:
            assessments_due.append({
                'obj': p, 'reason': "退院準備・サマリー作成", 
                'status': "退院準備", 'color': "info", 'is_discharge': True
            })
            # 30回目の評価も必要
            is_assessed = Assessment.objects.filter(patient=p, date=target_date).exists()
            assessments_due.append({
                'obj': p, 'reason': "終了時評価 (30回)", 
                'status': "実施済" if is_assessed else "実施未", 
                'color': "success" if is_assessed else "danger", 'timing_code': 'week6'
            })
            continue # 治療タブには追加しない

        if session_num <= 0 or session_num > 35: continue
        
        today_session = TreatmentSession.objects.filter(patient=p, date__date=target_date).first()
        is_done = today_session is not None
        
        treatments.append({
            'obj': p, 'note': f"第{session_num}回",
            'status': "実施済" if is_done else "実施未",
            'color': "success" if is_done else "danger",
            'session_num': session_num
        })

        if session_num == 15:
            is_assessed = Assessment.objects.filter(patient=p, date=target_date).exists()
            assessments_due.append({'obj': p, 'reason': "中間評価 (15回)", 'status': "実施済" if is_assessed else "実施未", 'color': "success" if is_assessed else "danger", 'timing_code': 'week3'})

    return render(request, 'rtms_app/dashboard.html', {
        'today': target_date, 'prev_day': prev_day, 'next_day': next_day,
        'new_patients': new_patients, 'admissions': admissions,
        'mappings': mappings, 'treatments': treatments,
        'assessments_due': assessments_due,
    })

@login_required
def patient_list_view(request):
    patients = Patient.objects.all().order_by('card_id')
    return render(request, 'rtms_app/patient_list.html', {'patients': patients})

@login_required
def admission_procedure(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    if request.method == 'POST':
        form = AdmissionProcedureForm(request.POST, instance=patient)
        if form.is_valid():
            proc = form.save(commit=False)
            proc.is_admission_procedure_done = True
            proc.save()
            return redirect('dashboard')
    else: form = AdmissionProcedureForm(instance=patient)
    return render(request, 'rtms_app/admission_procedure.html', {'patient': patient, 'form': form})

@login_required
def mapping_add(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    history = MappingSession.objects.filter(patient=patient).order_by('date')
    if request.method == 'POST':
        form = MappingForm(request.POST)
        if form.is_valid():
            m = form.save(commit=False)
            m.patient = patient
            m.save()
            return redirect('dashboard')
    else:
        initial_date = request.GET.get('date') or timezone.now().date()
        form = MappingForm(initial={'date': initial_date, 'week_number': 1})
    return render(request, 'rtms_app/mapping_add.html', {'patient': patient, 'form': form, 'history': history})

@login_required
def patient_first_visit(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    referral_options = Patient.objects.values_list('referral_source', flat=True).distinct()
    referral_options = [r for r in referral_options if r]
    
    end_date_est = get_completion_date(patient.first_treatment_date)

    if request.method == 'POST':
        form = PatientFirstVisitForm(request.POST, instance=patient)
        if form.is_valid():
            p = form.save(commit=False)
            diag_list = request.POST.getlist('diag_list')
            diag_other = request.POST.get('diag_other', '').strip()
            # 診断名の保存処理 (リスト結合)
            # 既存のロジックに合わせて実装
            full_diagnosis = ", ".join(diag_list)
            if diag_other: 
                if full_diagnosis: full_diagnosis += f", その他({diag_other})"
                else: full_diagnosis = f"その他({diag_other})"
            p.diagnosis = full_diagnosis
            p.save()
            return redirect('dashboard')
    else:
        form = PatientFirstVisitForm(instance=patient)
        
    return render(request, 'rtms_app/patient_first_visit.html', {
        'patient': patient, 'form': form, 'referral_options': referral_options,
        'end_date_est': end_date_est 
    })

@login_required
def treatment_add(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    latest_mapping = MappingSession.objects.filter(patient=patient).order_by('-date').first()
    side_effect_items = [
        ('headache', '頭痛'), ('scalp', '頭皮痛（刺激痛）'), ('discomfort', '刺激部位の不快感'),
        ('tooth', '歯痛'), ('twitch', '顔面のけいれん'), ('dizzy', 'めまい'),
        ('nausea', '吐き気'), ('tinnitus', '耳鳴り'), ('hearing', '聴力低下'),
        ('anxiety', '不安感・焦燥感'), ('other', 'その他'),
    ]
    
    target_date_str = request.GET.get('date')
    now = timezone.now()
    if target_date_str:
        t = parse_date(target_date_str)
        if t: initial_date = now.replace(year=t.year, month=t.month, day=t.day)
        else: initial_date = now
    else: initial_date = now
    
    session_num = get_session_number(patient.first_treatment_date, initial_date.date())
    end_date_est = get_completion_date(patient.first_treatment_date)

    if request.method == 'POST':
        form = TreatmentForm(request.POST)
        if form.is_valid():
            s = form.save(commit=False)
            s.patient = patient
            s.performer = request.user
            se_data = {}
            for key, label in side_effect_items:
                val = request.POST.get(f'se_{key}')
                if val: se_data[key] = val
            se_data['note'] = request.POST.get('se_note', '')
            s.side_effects = se_data
            s.save()
            return redirect('dashboard')
    else:
        initial_data = {'date': initial_date, 'total_pulses': 1980, 'intensity': 120}
        if latest_mapping: initial_data['motor_threshold'] = latest_mapping.resting_mt
        form = TreatmentForm(initial=initial_data)

    return render(request, 'rtms_app/treatment_add.html', {
        'patient': patient, 'form': form, 'latest_mapping': latest_mapping, 
        'side_effect_items': side_effect_items,
        'session_num': session_num, 'end_date_est': end_date_est, 'start_date': patient.first_treatment_date
    })

@login_required
def assessment_add(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    history = Assessment.objects.filter(patient=patient).order_by('date')
    hamd_items = [
        ('q1', '1. 抑うつ気分', 4, "0. なし<br>1. 質問をされた時のみ示される..."),
        ('q2', '2. 罪責感', 4, "0. なし<br>1. 自己非難..."),
        ('q3', '3. 自殺', 4, "0. なし..."),
        ('q4', '4. 入眠障害', 2, "0. 入眠困難はない..."),
        ('q5', '5. 熟眠障害', 2, "0. 熟眠困難はない..."),
        ('q6', '6. 早朝睡眠障害', 2, "0. 早朝睡眠に困難はない..."),
        ('q7', '7. 仕事と活動', 4, "0. 困難なくできる..."),
        ('q8', '8. 精神運動抑制', 4, "0. 発話・思考は正常である..."),
        ('q9', '9. 精神運動激越', 4, "0. なし..."),
        ('q10', '10. 不安, 精神症状', 4, "0. 問題なし..."),
        ('q11', '11. 不安, 身体症状', 4, "0. なし..."),
        ('q12', '12. 身体症状, 消化器系', 2, "0. なし..."),
        ('q13', '13. 身体症状, 一般的', 2, "0. なし..."),
        ('q14', '14. 生殖器症状', 2, "0. なし..."),
        ('q15', '15. 心気症', 4, "0. なし..."),
        ('q16', '16. 体重減少', 2, "0. 体重減少なし..."),
        ('q17', '17. 病識', 2, "0. うつ状態であり病気であることを認める..."),
        ('q18', '18. 日内変動', 2, "<strong>A. 変動の有無</strong>..."),
        ('q19', '19. 現実感喪失, 離人症', 4, "0. なし..."),
        ('q20', '20. 妄想症状', 3, "0. なし..."),
        ('q21', '21. 強迫症状', 2, "0. なし..."),
    ]

    target_date_str = request.GET.get('date') or timezone.now().strftime('%Y-%m-%d')
    timing = request.GET.get('timing', 'other')
    
    existing_assessment = Assessment.objects.filter(
        patient=patient, 
        date=target_date_str, 
        type='HAM-D'
    ).first()

    recommendation = ""
    if timing in ['week3', 'week6']:
        baseline = Assessment.objects.filter(patient=patient, timing='baseline').first()
        if baseline and baseline.total_score_21 > 0 and existing_assessment:
            imp = (baseline.total_score_21 - existing_assessment.total_score_21) / baseline.total_score_21 * 100
            if imp < 20: recommendation = "【判定】反応不良 (改善率20%未満)。刺激部位やプロトコルの変更を検討してください。"
            else: recommendation = "【判定】治療継続 (順調に改善中)。"

    if request.method == 'POST':
        try:
            scores = {}
            for key, label, max_score, text in hamd_items:
                scores[key] = int(request.POST.get(key, 0))
            
            if existing_assessment:
                assessment = existing_assessment
                assessment.scores = scores
                assessment.timing = request.POST.get('timing', 'other')
                assessment.note = request.POST.get('note', '')
            else:
                assessment = Assessment(
                    patient=patient,
                    date=target_date_str,
                    type='HAM-D',
                    scores=scores,
                    timing=request.POST.get('timing', 'other'),
                    note=request.POST.get('note', '')
                )
            
            assessment.calculate_scores()
            assessment.save()
            return redirect('dashboard')
        except Exception as e: print(e)

    return render(request, 'rtms_app/assessment_add.html', {
        'patient': patient, 'history': history, 'today': target_date_str, 
        'hamd_items': hamd_items, 'initial_timing': timing,
        'existing_assessment': existing_assessment, 
        'recommendation': recommendation
    })

@login_required
def patient_summary_view(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    sessions = TreatmentSession.objects.filter(patient=patient).order_by('date')
    assessments = Assessment.objects.filter(patient=patient).order_by('date')
    
    test_scores = assessments 

    score_admin = assessments.first()
    score_w3 = assessments.filter(timing='week3').first()
    score_w6 = assessments.filter(timing='week6').first()
    def fmt_score(obj): return f"HAMD17 {obj.total_score_17}点 HAMD21 {obj.total_score_21}点" if obj else "未評価"

    side_effects_list_all = []
    history_list = []
    SE_MAP = {'headache': '頭痛', 'scalp': '頭皮痛', 'discomfort': '不快感', 'tooth': '歯痛', 'twitch': '攣縮', 'dizzy': 'めまい', 'nausea': '吐き気', 'tinnitus': '耳鳴り', 'hearing': '聴力低下', 'anxiety': '不安', 'other': 'その他'}

    for i, s in enumerate(sessions, 1):
        se_text = []
        if s.side_effects:
            for k, v in s.side_effects.items():
                if k != 'note' and v and str(v) != '0':
                    se_text.append(SE_MAP.get(k, k))
                    side_effects_list_all.append(SE_MAP.get(k, k))
        history_list.append({'count': i, 'date': s.date, 'mt': s.motor_threshold, 'intensity': s.intensity, 'se': "、".join(se_text) if se_text else "なし"})

    side_effects_summary = ", ".join(list(set(side_effects_list_all))) if side_effects_list_all else "特になし"
    start_date_str = sessions.first().date.strftime('%Y年%m月%d日') if sessions.exists() else "未開始"
    end_date_str = sessions.last().date.strftime('%Y年%m月%d日') if sessions.exists() else "未終了"
    total_count = sessions.count()
    admission_date_str = patient.admission_date.strftime('%Y年%m月%d日') if patient.admission_date else "不明"
    created_at_str = patient.created_at.strftime('%Y年%m月%d日')
    
    summary_text = (
        f"{created_at_str}初診、{admission_date_str}任意入院。\n"
        f"入院時{fmt_score(score_admin)}、{start_date_str}から全{total_count}回のrTMS治療を実施した。\n"
        f"3週時、{fmt_score(score_w3)}、6週時、{fmt_score(score_w6)}となった。\n"
        f"治療中の合併症：{side_effects_summary}。\n"
        f"{end_date_str}退院。紹介元へ逆紹介、抗うつ薬の治療継続を依頼した。"
    )

    return render(request, 'rtms_app/patient_summary.html', {
        'patient': patient, 
        'summary_text': summary_text, 
        'history_list': history_list, 
        'today': timezone.now().date(),
        'test_scores': test_scores, 
    })

@login_required
def patient_add_view(request):
    if request.method == 'POST':
        form = PatientRegistrationForm(request.POST)
        if form.is_valid(): form.save(); return redirect('dashboard')
    else: form = PatientRegistrationForm()
    return render(request, 'rtms_app/patient_add.html', {'form': form})

@login_required
def export_treatment_csv(request):
    response = HttpResponse(content_type='text/csv; charset=utf-8-sig')
    response['Content-Disposition'] = 'attachment; filename="treatment_data.csv"'
    writer = csv.writer(response)
    writer.writerow(['ID', '氏名', '実施日時', 'MT(%)', '強度(%)', 'パルス数', '実施者', '副作用'])
    treatments = TreatmentSession.objects.all().select_related('patient', 'performer').order_by('date')
    for t in treatments:
        se_str = json.dumps(t.side_effects, ensure_ascii=False) if t.side_effects else ""
        writer.writerow([t.patient.card_id, t.patient.name, t.date.strftime('%Y-%m-%d %H:%M'), t.motor_threshold, t.intensity, t.total_pulses, t.performer.username if t.performer else "", se_str])
    return response

@login_required
def download_db(request):
    if not request.user.is_staff: return HttpResponse("Forbidden", 403)
    db_path = settings.DATABASES['default']['NAME']
    if os.path.exists(db_path): return FileResponse(open(db_path, 'rb'), as_attachment=True, filename='db.sqlite3')
    return HttpResponse("Not found", 404)

def custom_logout_view(request):
    logout(request)
    return redirect('/admin/login/')
    
def patient_print_preview(request, pk):
    patient = get_object_or_404(Patient, pk=pk)
    
    # 終了予定日の計算
    end_date_est = get_completion_date(patient.first_treatment_date)

    context = {
        'patient': patient,
        'end_date_est': end_date_est
    }
    return render(request, 'rtms_app/print_preview.html', context)

def patient_print_summary(request, pk):
    patient = get_object_or_404(Patient, pk=pk)
    mode = request.GET.get('mode', 'summary')
    
    test_scores = Assessment.objects.filter(patient=patient).order_by('date')
    
    context = {
        'patient': patient,
        'mode': mode,
        'today': datetime.date.today(), # datetimeをimportしたので動きます
        'test_scores': test_scores,
    }
    return render(request, 'rtms_app/print_summary.html', context)