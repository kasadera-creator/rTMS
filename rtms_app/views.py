from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time
from datetime import timedelta, datetime
import datetime as dt_module # datetimeモジュール自体を別名で
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

def get_current_week_number(start_date, target_date):
    """
    開始日を起点として、ターゲット日が第何週目かを返す。
    例: 開始日〜開始日+6日 -> 第1週
    """
    if not start_date or target_date < start_date: return 0
    days_diff = (target_date - start_date).days
    return (days_diff // 7) + 1

def get_session_count(patient, target_date=None):
    """指定日時点での治療回数を取得"""
    query = TreatmentSession.objects.filter(patient=patient)
    if target_date:
        query = query.filter(date__date__lte=target_date)
    return query.count()

def get_weekly_session_count(patient, target_date):
    """
    ターゲット日が含まれる週（開始日から起算した7日間区切り）の治療回数を取得
    """
    if not patient.first_treatment_date: return 0
    
    start_date = patient.first_treatment_date
    days_diff = (target_date - start_date).days
    week_start_offset = (days_diff // 7) * 7
    
    week_start_date = start_date + timedelta(days=week_start_offset)
    week_end_date = week_start_date + timedelta(days=6)
    
    return TreatmentSession.objects.filter(
        patient=patient,
        date__date__range=[week_start_date, week_end_date]
    ).count()

def get_completion_date(start_date):
    """30回目（終了予定日）を計算（平日のみカウントの簡易版）"""
    if not start_date: return None
    current = start_date
    count = 0
    while count < 30:
        if current.weekday() < 5: count += 1
        if count == 30: return current
        current += timedelta(days=1)
    return current

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
    
    # A. 治療前評価 (入院日〜治療開始日まで)
    pre_candidates = Patient.objects.filter(
        admission_date__lte=target_date
    ).filter(
        Q(first_treatment_date__isnull=True) | Q(first_treatment_date__gte=target_date)
    )
    
    for p in pre_candidates:
        done = Assessment.objects.filter(patient=p, timing='baseline').exists()
        if not done:
            task_assessment.append({'obj': p, 'status': "実施未", 'color': "danger", 'timing_code': 'baseline', 'todo': "治療前評価"})
        elif Assessment.objects.filter(patient=p, timing='baseline', date=target_date).exists():
             task_assessment.append({'obj': p, 'status': "実施済", 'color': "success", 'timing_code': 'baseline', 'todo': "治療前評価 (完了)"})

    # B. 治療期間中のタスク
    active_candidates = Patient.objects.filter(first_treatment_date__lte=target_date).order_by('card_id')
    
    for p in active_candidates:
        # 開始日起算の週数
        week_num = get_current_week_number(p.first_treatment_date, target_date)
        session_count_so_far = get_session_count(p, target_date)

        # 30回完了後は退院準備のみ（シンプル化のためここでは30回目以降は表示しないか、退院準備へ誘導）
        if session_count_so_far >= 30:
            # 本日が30回目実施日なら表示
             pass

        # --- 4. 治療実施 (平日のみ) ---
        if target_date.weekday() < 5 and session_count_so_far < 30:
            today_session = TreatmentSession.objects.filter(patient=p, date__date=target_date).first()
            is_done = today_session is not None
            current_count = session_count_so_far if is_done else session_count_so_far + 1
            
            task_treatment.append({
                'obj': p, 
                'note': f"第{week_num}週 ({current_count}回目)", 
                'status': "実施済" if is_done else "実施未", 
                'color': "success" if is_done else "danger", 
                'session_num': current_count, 
                'todo': "rTMS治療"
            })

        # --- 5. 尺度評価 (算定要件: 第3週、第6週) ---
        target_timing = None
        todo_label = ""
        
        # 第3週目 (15日目〜21日目)
        if week_num == 3:
            target_timing = 'week3'
            todo_label = "中間評価 (第3週)"
        # 第6週目 (36日目〜42日目)
        elif week_num == 6:
            target_timing = 'week6'
            todo_label = "最終評価 (第6週)"
            
        if target_timing:
            # この週に既に評価済みかチェック
            # (週の範囲を取得)
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
                # 未実施なら表示
                task_assessment.append({'obj': p, 'status': "実施未", 'color': "danger", 'timing_code': target_timing, 'todo': todo_label})

        # --- 6. 退院準備 ---
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
def treatment_add(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date')
    latest_mapping = MappingSession.objects.filter(patient=patient).order_by('-date').first()
    side_effect_items = [('headache', '頭痛'), ('scalp', '頭皮痛（刺激痛）'), ('discomfort', '刺激部位の不快感'), ('tooth', '歯痛'), ('twitch', '顔面のけいれん'), ('dizzy', 'めまい'), ('nausea', '吐き気'), ('tinnitus', '耳鳴り'), ('hearing', '聴力低下'), ('anxiety', '不安感・焦燥感'), ('other', 'その他')]
    
    # 日付設定
    target_date_str = request.GET.get('date')
    now = timezone.localtime(timezone.now()) # JST
    if target_date_str:
        t_date = parse_date(target_date_str)
        initial_date = t_date
    else:
        initial_date = now.date()

    # 治療回数と週数
    session_num = get_session_count(patient, initial_date) + 1
    week_num = get_current_week_number(patient.first_treatment_date, initial_date)
    end_date_est = get_completion_date(patient.first_treatment_date)

    # --- 判定とメッセージ ---
    alert_msg = ""
    instruction_msg = ""
    is_remission = False
    
    # 3週目の評価結果を取得
    last_assessment = Assessment.objects.filter(patient=patient, timing='week3').order_by('-date').first()
    baseline_assessment = Assessment.objects.filter(patient=patient, timing='baseline').order_by('-date').first()
    
    judgment_info = None

    if last_assessment:
        score_now = last_assessment.total_score_17
        
        # 判定ロジック
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

        # 漸減チェック (寛解の場合のみ)
        if is_remission and week_num >= 4:
            weekly_count = get_weekly_session_count(patient, initial_date)
            # 今回の分(+1)を含めて制限を超えるかチェックするか、現状を表示するか
            # ここでは「今回の登録で何回目になるか」を表示
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
                alert_msg = "【禁止】第7週以降のため、原則として治療は算定できません。"

    if request.method == 'POST':
        form = TreatmentForm(request.POST)
        if form.is_valid():
            s = form.save(commit=False)
            s.patient = patient
            s.performer = request.user
            
            # 日付と時間を結合して保存
            d = form.cleaned_data['treatment_date']
            t = form.cleaned_data['treatment_time']
            dt = datetime.datetime.combine(d, t)
            # タイムゾーン付与
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
        # 初期値: 時間は現在時刻
        initial_data = {
            'treatment_date': initial_date,
            'treatment_time': now.strftime('%H:%M'),
            'total_pulses': 1980, 
            'intensity': 120
        }
        if latest_mapping: initial_data['motor_threshold'] = latest_mapping.resting_mt
        form = TreatmentForm(initial=initial_data)

    return render(request, 'rtms_app/treatment_add.html', {
        'patient': patient, 'form': form, 
        'latest_mapping': latest_mapping, 'side_effect_items': side_effect_items, 
        'session_num': session_num, 'week_num': week_num,
        'end_date_est': end_date_est, 'start_date': patient.first_treatment_date, 
        'dashboard_date': dashboard_date,
        'alert_msg': alert_msg, 
        'instruction_msg': instruction_msg,
        'judgment_info': judgment_info
    })

# ... (patient_first_visit, assessment_add, その他のビューは次のセクションで修正) ...
@login_required
def patient_first_visit(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date')
    
    # 連携データ
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
                for key, _, _, _ in hamd_items:
                    scores[key] = int(request.POST.get(key, 0))
                
                if baseline_assessment:
                    assessment = baseline_assessment
                    assessment.scores = scores
                else:
                    assessment = Assessment(patient=patient, date=timezone.now().date(), type='HAM-D', scores=scores, timing='baseline')
                
                assessment.calculate_scores()
                assessment.save()
                
                total = assessment.total_score_17
                msg = ""
                severity = ""
                if 14 <= total <= 18:
                    severity = "中等症"
                    msg = "中等症と判定しました。rTMS適正質問票を確認してください。"
                elif total >= 19:
                    severity = "重症"
                    msg = "重症と判定しました。"
                elif 8 <= total <= 13:
                    severity = "軽症"
                else:
                    severity = "正常"

                return JsonResponse({'status': 'success', 'total_17': total, 'severity': severity, 'message': msg})
            except Exception as e:
                return JsonResponse({'status': 'error', 'message': str(e)})

        form = PatientFirstVisitForm(request.POST, instance=patient)
        if form.is_valid():
            p = form.save(commit=False)
            diag_list = request.POST.getlist('diag_list')
            diag_other = request.POST.get('diag_other', '').strip()
            full_diagnosis = ", ".join(diag_list)
            if diag_other: 
                if full_diagnosis: full_diagnosis += f", その他({diag_other})"
                else: full_diagnosis = f"その他({diag_other})"
            p.diagnosis = full_diagnosis
            p.save()
            return redirect(f"/app/dashboard/?date={dashboard_date}" if dashboard_date else 'dashboard')
    else:
        form = PatientFirstVisitForm(instance=patient)
        
    return render(request, 'rtms_app/patient_first_visit.html', {
        'patient': patient, 'form': form, 'referral_options': referral_options, 
        'referral_map_json': json.dumps(referral_map_json, ensure_ascii=False),
        'end_date_est': end_date_est,
        'dashboard_date': dashboard_date,
        'hamd_items_left': hamd_items_left,
        'hamd_items_right': hamd_items_right,
        'baseline_assessment': baseline_assessment
    })

# (assessment_add, patient_summary_view などは前回提示分とロジックは同じため省略しますが、必要なら再提示します)
# 一旦、上記の修正部分のみ反映してください。