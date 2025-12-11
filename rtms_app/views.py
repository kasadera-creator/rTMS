from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.utils.dateparse import parse_date
from datetime import timedelta, date
import datetime
from django.http import HttpResponse, FileResponse, JsonResponse
from django.conf import settings
from django.contrib.auth import logout
from django.db.models import Q
import os
import csv
import json

from .models import Patient, TreatmentSession, MappingSession, Assessment
from .forms import (
    PatientFirstVisitForm, MappingForm, TreatmentForm, 
    PatientRegistrationForm, AdmissionProcedureForm
)

# --- ヘルパー関数 ---
def get_session_number(start_date, target_date):
    if not start_date or target_date < start_date: return 0
    if target_date.weekday() >= 5: return -1
    current = start_date
    count = 0
    while current <= target_date:
        if current.weekday() < 5: count += 1
        current += timedelta(days=1)
    return count

def get_completion_date(start_date):
    if not start_date: return None
    return get_date_of_session(start_date, 30)
    
def get_date_of_session(start_date, target_session_num):
    if not start_date or target_session_num <= 0: return None
    current = start_date
    count = 1 if current.weekday() < 5 else 0
    while count < target_session_num:
        current += timedelta(days=1)
        if current.weekday() < 5: count += 1
    return current

# カレンダーデータ生成ロジック (共通)
def generate_calendar_data(patient):
    # 期間設定: 入院日or治療開始日or今日 〜 退院日or終了予定+1日
    start_date = patient.admission_date or patient.first_treatment_date or timezone.now().date()
    
    # 終了日: 退院日が設定されていればそれ、なければ30回目予定日の翌日
    end_date = patient.discharge_date
    if not end_date:
        est_end = get_completion_date(patient.first_treatment_date)
        if est_end:
            end_date = est_end + timedelta(days=1) # 翌日を退院予定とする
        else:
            end_date = start_date + timedelta(days=60)

    calendar_days = []
    current = start_date
    treatment_start = patient.first_treatment_date
    mapping_dates = list(MappingSession.objects.filter(patient=patient).values_list('date', flat=True))
    
    # 実際の実施済み治療日を取得
    treatments_done = {t.date.date(): t for t in TreatmentSession.objects.filter(patient=patient)}

    while current <= end_date:
        day_info = {
            'date': current,
            'weekday': ["月", "火", "水", "木", "金", "土", "日"][current.weekday()],
            'events': [],
            'is_weekend': current.weekday() >= 5
        }
        
        # 1. 入院
        if current == patient.admission_date:
            day_info['events'].append({'type': 'admission', 'label': '入院'})
            
        # 2. 位置決め
        if current == patient.mapping_date or current in mapping_dates:
            day_info['events'].append({'type': 'mapping', 'label': '位置決め(MT測定)'})
            
        # 3. 治療 (実施済み または 予定)
        session_num = 0
        if treatment_start:
            session_num = get_session_number(treatment_start, current)
            
            # 平日かつ1-30回の範囲
            if session_num > 0 and session_num <= 30:
                status_label = ""
                # 実施済みチェック
                if current in treatments_done:
                    status_label = " (完了)"
                
                day_info['events'].append({'type': 'treatment', 'label': f'rTMS治療 {session_num}回目{status_label}'})
                
                # 4. 評価予定
                if session_num == 1:
                    day_info['events'].append({'type': 'assessment', 'label': '治療前評価'})
                elif session_num == 15:
                    day_info['events'].append({'type': 'assessment', 'label': '中間評価'})
                elif session_num == 30:
                    day_info['events'].append({'type': 'assessment', 'label': '最終評価'})
        
        # 5. 退院 (設定された日、または30回目翌日)
        if current == patient.discharge_date:
             day_info['events'].append({'type': 'discharge', 'label': '退院'})
        elif not patient.discharge_date and treatment_start and session_num == 30:
             # 30回目翌日を仮の退院予定として表示する場合、ループの次の日で判定するが、
             # ここでは簡易的に「30回目」の次の行で処理される
             pass
        
        # 30回目翌日の自動退院予定表示（退院日未定の場合）
        if not patient.discharge_date and treatment_start:
             est_end = get_completion_date(treatment_start)
             if est_end and current == est_end + timedelta(days=1):
                 day_info['events'].append({'type': 'discharge', 'label': '退院予定'})

        calendar_days.append(day_info)
        current += timedelta(days=1)
        
    return calendar_days


# --- ビュー関数 ---

@login_required
def dashboard_view(request):
    jst_now = timezone.localtime(timezone.now())
    if 'date' not in request.GET:
        return redirect(f'{request.path}?date={jst_now.strftime("%Y-%m-%d")}')
    try: target_date = parse_date(request.GET.get('date'))
    except: target_date = jst_now.date()
    if not target_date: target_date = jst_now.date()

    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    target_date_display = f"{target_date.year}年{target_date.month}月{target_date.day}日 ({weekdays[target_date.weekday()]})"

    prev_day = target_date - timedelta(days=1)
    next_day = target_date + timedelta(days=1)

    # 1. 初診
    task_first_visit = [{'obj': p, 'status': "診察済", 'todo': "初診"} for p in Patient.objects.filter(created_at__date=target_date)]

    # 2. 入院
    task_admission = []
    for p in Patient.objects.filter(admission_date=target_date):
        status = "手続済" if p.is_admission_procedure_done else "要手続"
        color = "success" if p.is_admission_procedure_done else "warning"
        task_admission.append({'obj': p, 'status': status, 'color': color, 'todo': "入院手続き"})

    # 3. 位置決め
    task_mapping = []
    for p in Patient.objects.filter(mapping_date=target_date):
        is_done = MappingSession.objects.filter(patient=p, date=target_date).exists()
        task_mapping.append({'obj': p, 'status': "実施済" if is_done else "実施未", 'color': "success" if is_done else "danger", 'todo': "MT測定"})

    # 4, 5, 6
    task_treatment = []
    task_assessment = []
    task_discharge = []
    
    # A. 治療前評価
    pre_candidates = Patient.objects.filter(admission_date__lte=target_date).filter(Q(first_treatment_date__isnull=True) | Q(first_treatment_date__gte=target_date))
    for p in pre_candidates:
        done = Assessment.objects.filter(patient=p, timing='baseline').exists()
        if not done:
            task_assessment.append({'obj': p, 'status': "実施未", 'color': "danger", 'timing_code': 'baseline', 'todo': "治療前評価"})
        elif Assessment.objects.filter(patient=p, timing='baseline', date=target_date).exists():
             task_assessment.append({'obj': p, 'status': "実施済", 'color': "success", 'timing_code': 'baseline', 'todo': "治療前評価 (完了)"})

    # B. 治療期間中のタスク
    active_candidates = Patient.objects.filter(first_treatment_date__lte=target_date).order_by('card_id')
    for p in active_candidates:
        week_num = get_current_week_number(p.first_treatment_date, target_date)
        session_count_so_far = get_session_count(p, target_date)

        if session_count_so_far >= 30: pass 

        # 4. 治療
        if target_date.weekday() < 5 and session_count_so_far < 30:
            today_session = TreatmentSession.objects.filter(patient=p, date__date=target_date).first()
            is_done = today_session is not None
            current_count = session_count_so_far if is_done else session_count_so_far + 1
            task_treatment.append({'obj': p, 'note': f"第{week_num}週 ({current_count}回目)", 'status': "実施済" if is_done else "実施未", 'color': "success" if is_done else "danger", 'session_num': current_count, 'todo': "rTMS治療"})

        # 5. 評価 (第3週、第6週)
        target_timing = None
        todo_label = ""
        if week_num == 3:
            target_timing = 'week3'; todo_label = "中間評価 (第3週)"
        elif week_num == 6:
            target_timing = 'week6'; todo_label = "最終評価 (第6週)"
            
        if target_timing:
            start_date = p.first_treatment_date
            days_diff = (target_date - start_date).days
            week_start_offset = (days_diff // 7) * 7
            ws = start_date + timedelta(days=week_start_offset)
            we = ws + timedelta(days=6)
            assessment = Assessment.objects.filter(patient=p, timing=target_timing, date__range=[ws, we]).first()
            if assessment:
                if assessment.date == target_date:
                    task_assessment.append({'obj': p, 'status': "実施済", 'color': "success", 'timing_code': target_timing, 'todo': f"{todo_label} (完了)"})
            else:
                task_assessment.append({'obj': p, 'status': "実施未", 'color': "danger", 'timing_code': target_timing, 'todo': todo_label})

        # 6. 退院準備
        if session_count_so_far == 30:
             task_discharge.append({'obj': p, 'status': "退院準備", 'color': "info", 'todo': "サマリー・紹介状作成"})

    dashboard_tasks = [
        {'list': task_first_visit, 'title': "① 初診", 'color': "bg-g-1", 'icon': "fa-user-plus"},
        {'list': task_admission, 'title': "② 入院", 'color': "bg-g-2", 'icon': "fa-procedures"},
        {'list': task_mapping, 'title': "③ 位置決め", 'color': "bg-g-3", 'icon': "fa-crosshairs"},
        {'list': task_treatment, 'title': "④ 治療実施", 'color': "bg-g-4", 'icon': "fa-bolt"},
        {'list': task_assessment, 'title': "⑤ 尺度評価", 'color': "bg-g-5", 'icon': "fa-clipboard-check"},
        {'list': task_discharge, 'title': "⑥ 退院準備", 'color': "bg-g-6", 'icon': "fa-file-export"},
    ]

    return render(request, 'rtms_app/dashboard.html', {
        'today': target_date, 'target_date_display': target_date_display, 
        'prev_day': prev_day, 'next_day': next_day,
        'today_raw': jst_now.date(),
        'dashboard_tasks': dashboard_tasks, 
    })

@login_required
def patient_list_view(request):
    patients = Patient.objects.all().order_by('card_id')
    dashboard_date = request.GET.get('dashboard_date')
    return render(request, 'rtms_app/patient_list.html', {'patients': patients, 'dashboard_date': dashboard_date})

@login_required
def admission_procedure(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date')
    if request.method == 'POST':
        form = AdmissionProcedureForm(request.POST, instance=patient)
        if form.is_valid():
            proc = form.save(commit=False)
            proc.is_admission_procedure_done = True
            proc.save()
            return redirect(f"/app/dashboard/?date={dashboard_date}" if dashboard_date else 'dashboard')
    else: form = AdmissionProcedureForm(instance=patient)
    return render(request, 'rtms_app/admission_procedure.html', {'patient': patient, 'form': form, 'dashboard_date': dashboard_date})

@login_required
def mapping_add(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date')
    history = MappingSession.objects.filter(patient=patient).order_by('date')
    if request.method == 'POST':
        form = MappingForm(request.POST)
        if form.is_valid():
            m = form.save(commit=False)
            m.patient = patient
            m.save()
            return redirect(f"/app/dashboard/?date={dashboard_date}" if dashboard_date else 'dashboard')
    else:
        initial_date = request.GET.get('date') or timezone.now().date()
        form = MappingForm(initial={'date': initial_date, 'week_number': 1})
    return render(request, 'rtms_app/mapping_add.html', {'patient': patient, 'form': form, 'history': history, 'dashboard_date': dashboard_date})

@login_required
def patient_first_visit(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date')
    
    all_patients = Patient.objects.all()
    referral_map = {}
    referral_sources_set = set()
    for p in all_patients:
        if p.referral_source:
            referral_sources_set.add(p.referral_source)
            if p.referral_doctor:
                if p.referral_source not in referral_map: referral_map[p.referral_source] = set()
                referral_map[p.referral_source].add(p.referral_doctor)
    referral_map_json = {k: sorted(list(v)) for k, v in referral_map.items()}
    referral_options = sorted(list(referral_sources_set))
    end_date_est = get_completion_date(patient.first_treatment_date)

    hamd_items = [('q1', '1. 抑うつ気分', 4, ""), ('q2', '2. 罪責感', 4, ""), ('q3', '3. 自殺', 4, ""), ('q4', '4. 入眠障害', 2, ""), ('q5', '5. 熟眠障害', 2, ""), ('q6', '6. 早朝睡眠障害', 2, ""), ('q7', '7. 仕事と活動', 4, ""), ('q8', '8. 精神運動抑制', 4, ""), ('q9', '9. 精神運動激越', 4, ""), ('q10', '10. 不安, 精神症状', 4, ""), ('q11', '11. 不安, 身体症状', 4, ""), ('q12', '12. 身体症状, 消化器系', 2, ""), ('q13', '13. 身体症状, 一般的', 2, ""), ('q14', '14. 生殖器症状', 2, ""), ('q15', '15. 心気症', 4, ""), ('q16', '16. 体重減少', 2, ""), ('q17', '17. 病識', 2, ""), ('q18', '18. 日内変動', 2, ""), ('q19', '19. 現実感喪失, 離人症', 4, ""), ('q20', '20. 妄想症状', 3, ""), ('q21', '21. 強迫症状', 2, "")]
    hamd_items_left = hamd_items[:11]
    hamd_items_right = hamd_items[11:]
    baseline_assessment = Assessment.objects.filter(patient=patient, timing='baseline').first()

    if request.method == 'POST':
        if 'hamd_ajax' in request.POST:
            try:
                scores = {}
                for key, _, _, _ in hamd_items: scores[key] = int(request.POST.get(key, 0))
                if baseline_assessment: assessment = baseline_assessment; assessment.scores = scores
                else: assessment = Assessment(patient=patient, date=timezone.now().date(), type='HAM-D', scores=scores, timing='baseline')
                assessment.calculate_scores()
                assessment.save()
                total = assessment.total_score_17
                msg = ""; severity = ""
                if 14 <= total <= 18: severity = "中等症"; msg = "中等症と判定しました。rTMS適正質問票を確認してください。"
                elif total >= 19: severity = "重症"; msg = "重症と判定しました。"
                elif 8 <= total <= 13: severity = "軽症"
                else: severity = "正常"
                return JsonResponse({'status': 'success', 'total_17': total, 'severity': severity, 'message': msg})
            except Exception as e: return JsonResponse({'status': 'error', 'message': str(e)})

        form = PatientFirstVisitForm(request.POST, instance=patient)
        if form.is_valid():
            p = form.save(commit=False)
            diag_list = request.POST.getlist('diag_list')
            diag_other = request.POST.get('diag_other', '').strip()
            full_diagnosis = ", ".join(diag_list)
            if diag_other: full_diagnosis += f", その他({diag_other})"
            p.diagnosis = full_diagnosis
            p.save()
            return redirect(f"/app/dashboard/?date={dashboard_date}" if dashboard_date else 'dashboard')
    else:
        form = PatientFirstVisitForm(instance=patient)
        
    return render(request, 'rtms_app/patient_first_visit.html', {
        'patient': patient, 'form': form, 'referral_options': referral_options, 
        'referral_map_json': json.dumps(referral_map_json, ensure_ascii=False),
        'end_date_est': end_date_est, 'dashboard_date': dashboard_date,
        'hamd_items_left': hamd_items_left, 'hamd_items_right': hamd_items_right,
        'baseline_assessment': baseline_assessment
    })

@login_required
def treatment_add(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date')
    latest_mapping = MappingSession.objects.filter(patient=patient).order_by('-date').first()
    side_effect_items = [('headache', '頭痛'), ('scalp', '頭皮痛（刺激痛）'), ('discomfort', '刺激部位の不快感'), ('tooth', '歯痛'), ('twitch', '顔面のけいれん'), ('dizzy', 'めまい'), ('nausea', '吐き気'), ('tinnitus', '耳鳴り'), ('hearing', '聴力低下'), ('anxiety', '不安感・焦燥感'), ('other', 'その他')]
    
    target_date_str = request.GET.get('date')
    now = timezone.localtime(timezone.now())
    if target_date_str: t = parse_date(target_date_str); initial_date = t
    else: initial_date = now.date()
    
    session_num = get_session_count(patient, initial_date) + 1
    week_num = get_current_week_number(patient.first_treatment_date, initial_date)
    end_date_est = get_completion_date(patient.first_treatment_date)

    alert_msg = ""
    instruction_msg = ""
    is_remission = False
    
    last_assessment = Assessment.objects.filter(patient=patient, timing='week3').order_by('-date').first()
    baseline_assessment = Assessment.objects.filter(patient=patient, timing='baseline').order_by('-date').first()
    judgment_info = None

    if last_assessment:
        score_now = last_assessment.total_score_17
        if score_now <= 7:
            is_remission = True
            judgment_info = f"寛解 (HAM-D17: {score_now}点)"
            instruction_msg = "【指示】第4週以降は漸減プロトコルに従ってください。"
        else:
            if baseline_assessment and baseline_assessment.total_score_17 > 0:
                imp_rate = (baseline_assessment.total_score_17 - score_now) / baseline_assessment.total_score_17
                if imp_rate >= 0.2:
                    judgment_info = f"有効 (改善率 {int(imp_rate*100)}%)"
                    instruction_msg = "【指示】有効性あり。治療を継続してください。"
                else:
                    judgment_info = f"無効/反応不良 (改善率 {int(imp_rate*100)}%)"
                    instruction_msg = "【指示】治療未反応。続行または中止を検討してください。"
            else:
                judgment_info = f"判定不能 (Baseデータなし)"

        if is_remission and week_num >= 4:
            weekly_count = get_weekly_session_count(patient, initial_date)
            current_weekly = weekly_count + 1
            if week_num == 4:
                limit = 3
                if current_weekly > limit: alert_msg = f"【制限超過】第4週(週3回まで)です。今回で週{current_weekly}回目になります。"
                else: alert_msg = f"【漸減】第4週です。週3回まで (現在: 週{current_weekly}回目)"
            elif week_num == 5:
                limit = 2
                if current_weekly > limit: alert_msg = f"【制限超過】第5週(週2回まで)です。今回で週{current_weekly}回目になります。"
                else: alert_msg = f"【漸減】第5週です。週2回まで (現在: 週{current_weekly}回目)"
            elif week_num == 6:
                limit = 1
                if current_weekly > limit: alert_msg = f"【制限超過】第6週(週1回まで)です。今回で週{current_weekly}回目になります。"
                else: alert_msg = f"【漸減】第6週です。週1回まで (現在: 週{current_weekly}回目)"
            elif week_num >= 7:
                alert_msg = "【警告】第7週以降のため、原則として治療は算定できません。"

    if request.method == 'POST':
        form = TreatmentForm(request.POST)
        if form.is_valid():
            s = form.save(commit=False)
            s.patient = patient
            s.performer = request.user
            d = form.cleaned_data['treatment_date']
            t = form.cleaned_data['treatment_time']
            dt = datetime.datetime.combine(d, t)
            s.date = timezone.make_aware(dt)
            se_data = {}
            for key, label in side_effect_items:
                val = request.POST.get(f'se_{key}')
                if val: se_data[key] = val
            se_data['note'] = request.POST.get('se_note', '')
            s.side_effects = se_data
            s.save()
            return redirect(f"/app/dashboard/?date={dashboard_date}" if dashboard_date else 'dashboard')
    else:
        initial_data = {'treatment_date': initial_date, 'treatment_time': now.strftime('%H:%M'), 'total_pulses': 1980, 'intensity': 120}
        if latest_mapping: initial_data['motor_threshold'] = latest_mapping.resting_mt
        form = TreatmentForm(initial=initial_data)
    return render(request, 'rtms_app/treatment_add.html', {'patient': patient, 'form': form, 'latest_mapping': latest_mapping, 'side_effect_items': side_effect_items, 'session_num': session_num, 'week_num': week_num, 'end_date_est': end_date_est, 'start_date': patient.first_treatment_date, 'dashboard_date': dashboard_date, 'alert_msg': alert_msg, 'instruction_msg': instruction_msg, 'judgment_info': judgment_info})

# ... (他のビューは変更なし、省略) ...
@login_required
def assessment_add(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date')
    history = Assessment.objects.filter(patient=patient).order_by('date')
    hamd_items = [('q1', '1. 抑うつ気分', 4, "0. なし..."), ('q2', '2. 罪責感', 4, "0. なし..."), ('q3', '3. 自殺', 4, "0. なし..."), ('q4', '4. 入眠障害', 2, "0. 入眠困難はない..."), ('q5', '5. 熟眠障害', 2, "0. 熟眠困難はない..."), ('q6', '6. 早朝睡眠障害', 2, "0. 早朝睡眠に困難はない..."), ('q7', '7. 仕事と活動', 4, "0. 困難なくできる..."), ('q8', '8. 精神運動抑制', 4, "0. 発話・思考は正常である..."), ('q9', '9. 精神運動激越', 4, "0. なし..."), ('q10', '10. 不安, 精神症状', 4, "0. 問題なし..."), ('q11', '11. 不安, 身体症状', 4, "0. なし..."), ('q12', '12. 身体症状, 消化器系', 2, "0. なし..."), ('q13', '13. 身体症状, 一般的', 2, "0. なし..."), ('q14', '14. 生殖器症状', 2, "0. なし..."), ('q15', '15. 心気症', 4, "0. なし..."), ('q16', '16. 体重減少', 2, "0. 体重減少なし..."), ('q17', '17. 病識', 2, "0. うつ状態であり病気であることを認める..."), ('q18', '18. 日内変動', 2, "<strong>A. 変動の有無</strong>..."), ('q19', '19. 現実感喪失, 離人症', 4, "0. なし..."), ('q20', '20. 妄想症状', 3, "0. なし..."), ('q21', '21. 強迫症状', 2, "0. なし...")]
    
    mid_index = 11
    hamd_items_left = hamd_items[:mid_index]
    hamd_items_right = hamd_items[11:]

    target_date_str = request.GET.get('date') or timezone.now().strftime('%Y-%m-%d')
    timing = request.GET.get('timing', 'other')
    existing_assessment = Assessment.objects.filter(patient=patient, date=target_date_str, type='HAM-D').first()
    
    recommendation = ""
    if timing == 'week3':
        baseline = Assessment.objects.filter(patient=patient, timing='baseline').first()
        if existing_assessment and baseline:
            score_now = existing_assessment.total_score_17
            score_base = baseline.total_score_17
            if score_now <= 7:
                recommendation = f"【判定: 寛解】HAM-D17が{score_now}点(7点以下)です。第4週以降は漸減プロトコルへ移行してください。"
            elif score_base > 0:
                imp = (score_base - score_now) / score_base
                if imp >= 0.2: recommendation = f"【判定: 有効】改善率 {int(imp*100)}% (20%以上)。治療を継続してください。"
                else: recommendation = f"【判定: 無効/反応不良】改善率 {int(imp*100)}% (20%未満)。中止を検討してください。"
    
    if request.method == 'POST':
        try:
            scores = {}
            for key, label, max_score, text in hamd_items:
                scores[key] = int(request.POST.get(key, 0))
            if existing_assessment:
                assessment = existing_assessment
                assessment.scores = scores; assessment.timing = request.POST.get('timing', 'other'); assessment.note = request.POST.get('note', '')
            else:
                assessment = Assessment(patient=patient, date=target_date_str, type='HAM-D', scores=scores, timing=request.POST.get('timing', 'other'), note=request.POST.get('note', ''))
            assessment.calculate_scores()
            assessment.save()
            return redirect(f"/app/dashboard/?date={dashboard_date}" if dashboard_date else 'dashboard')
        except Exception as e: print(e)
        
    return render(request, 'rtms_app/assessment_add.html', {'patient': patient, 'history': history, 'today': target_date_str, 'hamd_items_left': hamd_items_left, 'hamd_items_right': hamd_items_right, 'initial_timing': timing, 'existing_assessment': existing_assessment, 'recommendation': recommendation, 'dashboard_date': dashboard_date})

@login_required
def patient_summary_view(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date')
    if request.method == 'POST':
        patient.summary_text = request.POST.get('summary_text', '')
        patient.discharge_prescription = request.POST.get('discharge_prescription', '')
        d_date = request.POST.get('discharge_date')
        if d_date: patient.discharge_date = parse_date(d_date)
        else: patient.discharge_date = None
        patient.save()
        if request.headers.get('x-requested-with') == 'XMLHttpRequest': return JsonResponse({'status': 'success'})
        return redirect(f"/app/dashboard/?date={dashboard_date}" if dashboard_date else 'dashboard')
    sessions = TreatmentSession.objects.filter(patient=patient).order_by('date')
    assessments = Assessment.objects.filter(patient=patient).order_by('date')
    test_scores = assessments 
    score_admin = assessments.first(); score_w3 = assessments.filter(timing='week3').first(); score_w6 = assessments.filter(timing='week6').first()
    def fmt_score(obj): return f"HAMD17 {obj.total_score_17}点 HAMD21 {obj.total_score_21}点" if obj else "未評価"
    side_effects_list_all = []
    history_list = []
    SE_MAP = {'headache': '頭痛', 'scalp': '頭皮痛', 'discomfort': '不快感', 'tooth': '歯痛', 'twitch': '攣縮', 'dizzy': 'めまい', 'nausea': '吐き気', 'tinnitus': '耳鳴り', 'hearing': '聴力低下', 'anxiety': '不安', 'other': 'その他'}
    for i, s in enumerate(sessions, 1):
        se_text = []
        if s.side_effects:
            for k, v in s.side_effects.items():
                if k != 'note' and v and str(v) != '0': se_text.append(SE_MAP.get(k, k)); side_effects_list_all.append(SE_MAP.get(k, k))
        history_list.append({'count': i, 'date': s.date, 'mt': s.motor_threshold, 'intensity': s.intensity, 'se': "、".join(se_text) if se_text else "なし"})
    side_effects_summary = ", ".join(list(set(side_effects_list_all))) if side_effects_list_all else "特になし"
    start_date_str = sessions.first().date.strftime('%Y年%m月%d日') if sessions.exists() else "未開始"
    if patient.discharge_date: end_date_str = patient.discharge_date.strftime('%Y年%m月%d日')
    elif sessions.exists(): end_date_str = sessions.last().date.strftime('%Y年%m月%d日')
    else: end_date_str = "未定"
    total_count = sessions.count()
    admission_date_str = patient.admission_date.strftime('%Y年%m月%d日') if patient.admission_date else "不明"
    created_at_str = patient.created_at.strftime('%Y年%m月%d日')
    if patient.summary_text: summary_text = patient.summary_text
    else: summary_text = (f"{created_at_str}初診、{admission_date_str}任意入院。\n" f"入院時{fmt_score(score_admin)}、{start_date_str}から全{total_count}回のrTMS治療を実施した。\n" f"3週時、{fmt_score(score_w3)}、6週時、{fmt_score(score_w6)}となった。\n" f"治療中の合併症：{side_effects_summary}。\n" f"{end_date_str}退院。紹介元へ逆紹介、抗うつ薬の治療継続を依頼した。")
    return render(request, 'rtms_app/patient_summary.html', {'patient': patient, 'summary_text': summary_text, 'history_list': history_list, 'today': timezone.now().date(), 'test_scores': test_scores, 'dashboard_date': dashboard_date})

@login_required
def patient_add_view(request):
    referral_options = Patient.objects.values_list('referral_source', flat=True).distinct()
    referral_options = [r for r in referral_options if r]
    if request.method == 'POST':
        form = PatientRegistrationForm(request.POST)
        card_id = request.POST.get('card_id')
        existing_patients = Patient.objects.filter(card_id=card_id).order_by('-course_number')
        if 'confirm_create' in request.POST and existing_patients.exists():
            latest = existing_patients.first()
            new_course_num = latest.course_number + 1
            new_patient = Patient(card_id=latest.card_id, name=latest.name, birth_date=latest.birth_date, gender=latest.gender, referral_source=request.POST.get('referral_source') or latest.referral_source, referral_doctor=request.POST.get('referral_doctor') or latest.referral_doctor, life_history=latest.life_history, past_history=latest.past_history, diagnosis=latest.diagnosis, course_number=new_course_num)
            new_patient.save()
            return redirect('dashboard')
        if existing_patients.exists():
            latest = existing_patients.first()
            return render(request, 'rtms_app/patient_add.html', {'form': form, 'referral_options': referral_options, 'existing_patient': latest, 'next_course_num': latest.course_number + 1})
        if form.is_valid(): form.save(); return redirect('dashboard')
    else: form = PatientRegistrationForm()
    return render(request, 'rtms_app/patient_add.html', {'form': form, 'referral_options': referral_options})

@login_required
def export_treatment_csv(request):
    response = HttpResponse(content_type='text/csv; charset=utf-8-sig'); response['Content-Disposition'] = 'attachment; filename="treatment_data.csv"'; writer = csv.writer(response); writer.writerow(['ID', '氏名', '実施日時', 'MT(%)', '強度(%)', 'パルス数', '実施者', '副作用'])
    treatments = TreatmentSession.objects.all().select_related('patient', 'performer').order_by('date')
    for t in treatments: se_str = json.dumps(t.side_effects, ensure_ascii=False) if t.side_effects else ""; writer.writerow([t.patient.card_id, t.patient.name, t.date.strftime('%Y-%m-%d %H:%M'), t.motor_threshold, t.intensity, t.total_pulses, t.performer.username if t.performer else "", se_str])
    return response

@login_required
def download_db(request):
    if not request.user.is_staff: return HttpResponse("Forbidden", 403)
    db_path = settings.DATABASES['default']['NAME']
    if os.path.exists(db_path): return FileResponse(open(db_path, 'rb'), as_attachment=True, filename='db.sqlite3')
    return HttpResponse("Not found", 404)

def custom_logout_view(request): logout(request); return redirect('/admin/login/')

# ★新規追加: クリニカルパス表示用ビュー
@login_required
def patient_clinical_path(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    start_date = patient.admission_date or patient.first_treatment_date or timezone.now().date()
    end_date = patient.discharge_date or get_completion_date(patient.first_treatment_date)
    if not end_date: end_date = start_date + timedelta(days=60)
    
    calendar_days = []
    current = start_date
    treatment_start = patient.first_treatment_date
    mapping_dates = list(MappingSession.objects.filter(patient=patient).values_list('date', flat=True))
    
    while current <= end_date:
        day_info = {
            'date': current,
            'weekday': ["月", "火", "水", "木", "金", "土", "日"][current.weekday()],
            'events': [],
            'is_weekend': current.weekday() >= 5
        }
        if current == patient.admission_date: day_info['events'].append({'type': 'admission', 'label': '【入院】'})
        if current == patient.mapping_date or current in mapping_dates: day_info['events'].append({'type': 'mapping', 'label': 'MT測定(位置決め)'})
        if treatment_start:
            session_num = get_session_number(treatment_start, current)
            if session_num > 0 and session_num <= 30:
                day_info['events'].append({'type': 'treatment', 'label': f'rTMS治療 ({session_num}回目)'})
                if session_num == 1: day_info['events'].append({'type': 'assessment', 'label': '治療前評価'})
                elif session_num == 15: day_info['events'].append({'type': 'assessment', 'label': '中間評価(3週)'})
                elif session_num == 30: day_info['events'].append({'type': 'assessment', 'label': '最終評価(6週)'})
        if current == patient.discharge_date: day_info['events'].append({'type': 'discharge', 'label': '【退院】'})
        calendar_days.append(day_info)
        current += timedelta(days=1)

    return render(request, 'rtms_app/patient_clinical_path.html', {'patient': patient, 'calendar_days': calendar_days, 'today': timezone.now().date()})

def patient_print_preview(request, pk): 
    patient = get_object_or_404(Patient, pk=pk)
    end_date_est = get_completion_date(patient.first_treatment_date)
    mode = request.GET.get('mode', 'summary')
    context = { 'patient': patient, 'end_date_est': end_date_est, 'mode': mode }
    return render(request, 'rtms_app/print_preview.html', context)

def patient_print_summary(request, pk): 
    patient = get_object_or_404(Patient, pk=pk)
    mode = request.GET.get('mode', 'summary')
    test_scores = Assessment.objects.filter(patient=patient).order_by('date')
    context = {'patient': patient, 'mode': mode, 'today': datetime.date.today(), 'test_scores': test_scores}
    return render(request, 'rtms_app/print_summary.html', context)
    
# ★新規: ブラウザ用パス表示ビュー
@login_required
def patient_clinical_path(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    calendar_days = generate_calendar_data(patient)
    return render(request, 'rtms_app/patient_clinical_path.html', {
        'patient': patient,
        'calendar_days': calendar_days,
        'today': timezone.now().date()
    })

# ★新規: 印刷用パスビュー
@login_required
def patient_print_path(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    calendar_days = generate_calendar_data(patient)
    return render(request, 'rtms_app/print_clinical_path.html', {
        'patient': patient,
        'calendar_days': calendar_days,
    })