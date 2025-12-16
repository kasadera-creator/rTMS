from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time
from django.urls import reverse
from django.templatetags.static import static
from datetime import timedelta, date
import datetime
from django.http import HttpResponse, FileResponse, JsonResponse
from django.conf import settings
from django.contrib.auth import logout
from django.db.models import Q
import os
import csv
import json
from urllib.parse import urlencode

from .models import Patient, TreatmentSession, MappingSession, Assessment, ConsentDocument, AuditLog
from .forms import (
    PatientFirstVisitForm, MappingForm, TreatmentForm, 
    PatientRegistrationForm, AdmissionProcedureForm
)
from .utils.request_context import get_current_request, get_client_ip, get_user_agent, can_view_audit

def log_audit_action(patient, action, target_model, target_pk, summary='', meta=None):
    request = get_current_request()
    if not request or not request.user.is_authenticated:
        return
    
    ip = get_client_ip(request)
    user_agent = get_user_agent(request)
    
    meta = meta or {}
    if patient:
        meta['course_number'] = getattr(patient, 'course_number', 1)
    
    AuditLog.objects.create(
        user=request.user,
        patient=patient,
        target_model=target_model,
        target_pk=target_pk,
        action=action,
        summary=summary,
        meta=meta,
        ip=ip,
        user_agent=user_agent,
    )

def build_url(name, args=None, query=None):
    """
    reverse() でURLを作り、必要なら query dict を安全に付与する。

    名前空間が付いていない場合は rtms_app: を補完する。
    """
    resolved_name = name if ":" in name else f"rtms_app:{name}"
    base = reverse(resolved_name, args=args)
    return f"{base}?{urlencode(query, doseq=True)}" if query else base
    
# ==========================================
# 祝日定義 (2024-2030) + 年末年始 (12/29-1/3)
# ==========================================
JP_HOLIDAYS = {
    date(2024, 1, 1), date(2024, 1, 8), date(2024, 2, 11), date(2024, 2, 12),
    date(2024, 2, 23), date(2024, 3, 20), date(2024, 4, 29), date(2024, 5, 3),
    date(2024, 5, 4), date(2024, 5, 5), date(2024, 5, 6), date(2024, 7, 15),
    date(2024, 8, 11), date(2024, 8, 12), date(2024, 9, 16), date(2024, 9, 22),
    date(2024, 9, 23), date(2024, 10, 14), date(2024, 11, 3), date(2024, 11, 4),
    date(2024, 11, 23),
    date(2025, 1, 1), date(2025, 1, 13), date(2025, 2, 11), date(2025, 2, 23),
    date(2025, 2, 24), date(2025, 3, 20), date(2025, 4, 29), date(2025, 5, 3),
    date(2025, 5, 4), date(2025, 5, 5), date(2025, 5, 6), date(2025, 7, 21),
    date(2025, 8, 11), date(2025, 9, 15), date(2025, 9, 23), date(2025, 10, 13),
    date(2025, 11, 3), date(2025, 11, 23), date(2025, 11, 24),
    date(2026, 1, 1), date(2026, 1, 12), date(2026, 2, 11), date(2026, 2, 23),
    date(2026, 3, 20), date(2026, 4, 29), date(2026, 5, 3), date(2026, 5, 4),
    date(2026, 5, 5), date(2026, 5, 6), date(2026, 7, 20), date(2026, 8, 11),
    date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23), date(2026, 10, 12),
    date(2026, 11, 3), date(2026, 11, 23),
}

def is_holiday(d):
    """日付が祝日リストまたは年末年始に含まれるか"""
    if d in JP_HOLIDAYS: return True
    if d.month == 12 and d.day >= 29: return True
    if d.month == 1 and d.day <= 3: return True
    return False

def is_treatment_day(d):
    """治療実施日か判定（平日かつ祝日でない）"""
    return d.weekday() < 5 and not is_holiday(d)

# =========================
# Assessment window helpers
# =========================
def _first_last_treatment_day_in_range(start_d, end_d):
    """start_d〜end_d の範囲で、治療日(is_treatment_day)の最初と最後を返す。無ければ (None, None)"""
    cur = start_d
    first = None
    last = None
    while cur <= end_d:
        if is_treatment_day(cur):
            if first is None:
                first = cur
            last = cur
        cur += timedelta(days=1)
    return first, last

def get_assessment_window(patient, timing):
    """
    評価予定日レンジ(window)を返す: (window_start, window_end)
    baseline: 初診日(created_at)〜初回治療日
    week3: 第3週(14-20日後)の治療日(平日・祝日除外)の最初〜最後
    week6: 第6週(35-41日後)の治療日(平日・祝日除外)の最初〜最後
    """
    # baseline
    if timing == "baseline":
        ws = patient.created_at.date() if patient.created_at else timezone.localdate()
        we = patient.first_treatment_date or ws
        return ws, we

    if not patient.first_treatment_date:
        today = timezone.localdate()
        return today, today

    ft = patient.first_treatment_date
    if timing == "week3":
        raw_start = ft + timedelta(days=14)
        raw_end = ft + timedelta(days=20)
    elif timing == "week6":
        raw_start = ft + timedelta(days=35)
        raw_end = ft + timedelta(days=41)
    else:
        today = timezone.localdate()
        return today, today

    ws, we = _first_last_treatment_day_in_range(raw_start, raw_end)
    # 治療日が1日も無い極端ケースのフォールバック
    if ws is None or we is None:
        ws = raw_start
        we = raw_end
    return ws, we

# --- ヘルパー関数 ---

def get_session_number(start_date, target_date):
    if not start_date or target_date < start_date: return 0
    if not is_treatment_day(target_date): return -1
    
    current = start_date
    count = 0
    while current <= target_date:
        if is_treatment_day(current):
            count += 1
        current += timedelta(days=1)
    return count

def get_date_of_session(start_date, target_session_num):
    if not start_date or target_session_num <= 0: return None
    current = start_date
    count = 1 if is_treatment_day(current) else 0
    
    while count < target_session_num:
        current += timedelta(days=1)
        if is_treatment_day(current):
            count += 1
    return current

def get_completion_date(start_date):
    """30回目（終了予定日）を計算"""
    if not start_date: return None
    return get_date_of_session(start_date, 30)

def get_current_week_number(start_date, target_date):
    if not start_date or target_date < start_date: return 0
    days_diff = (target_date - start_date).days
    return (days_diff // 7) + 1

def get_session_count(patient, target_date=None):
    query = TreatmentSession.objects.filter(patient=patient)
    if target_date:
        query = query.filter(date__date__lte=target_date)
    return query.count()

def get_weekly_session_count(patient, target_date):
    if not patient.first_treatment_date: return 0
    start_date = patient.first_treatment_date
    days_diff = (target_date - start_date).days
    week_start_offset = (days_diff // 7) * 7
    week_start_date = start_date + timedelta(days=week_start_offset)
    week_end_date = week_start_date + timedelta(days=6)
    return TreatmentSession.objects.filter(patient=patient, date__date__range=[week_start_date, week_end_date]).count()

def get_assessment_timing_for_date(patient, target_date):
    """
    指定日がどの評価タイミングに該当するか判定。
    baseline: 入院日 <= date < 治療開始日
    week3: 治療開始日を起点とした第3週 (14-20日目)
    week6: 治療開始日を起点とした第6週 (35-41日目)
    該当しない場合は None
    """
    if not patient.admission_date or not patient.first_treatment_date:
        return None
    
    if patient.admission_date <= target_date < patient.first_treatment_date:
        return 'baseline'
    
    if patient.first_treatment_date:
        days_since_start = (target_date - patient.first_treatment_date).days
        week_num = (days_since_start // 7) + 1
        if week_num == 3:
            return 'week3'
        elif week_num == 6:
            return 'week6'
    
    return None

def get_nth_treatment_date(first_treatment_date, n):
    """
    治療開始日からn日目の治療日を返す（平日、祝日除く）
    """
    current = first_treatment_date
    count = 0
    while count < n:
        if is_treatment_day(current):
            count += 1
            if count == n:
                return current
        current += timedelta(days=1)
    return None

def get_assessment_deadline(patient, timing):
    """
    指定 timing の評価期限最終日を返す。
    baseline: 治療開始日前日
    week3: 第3週の最終日 (治療開始日から15日目の治療日)
    week6: 第6週の最終日 (治療開始日から45日目の治療日)
    """
    if not patient.first_treatment_date:
        return None
    
    if timing == 'baseline':
        return patient.first_treatment_date - timedelta(days=1)
    elif timing == 'week3':
        return get_nth_treatment_date(patient.first_treatment_date, 15)
    elif timing == 'week6':
        return get_nth_treatment_date(patient.first_treatment_date, 45)
    return None

# ★修正: カレンダーデータ生成ロジック (週単位のリストを返す)
def generate_calendar_weeks(patient):
    # 基準となる開始日
    base_start = patient.admission_date or patient.first_treatment_date or timezone.now().date()
    
    # 基準となる終了日
    treatment_start = patient.first_treatment_date
    treatment_end_est = get_completion_date(treatment_start)
    
    base_end = patient.discharge_date
    if not base_end:
        if treatment_end_est:
            base_end = treatment_end_est + timedelta(days=14)  # 余白を14日に増やす
        else:
            base_end = base_start + timedelta(days=60)
            
    # 開始日が月曜になるように調整
    start_date = base_start - timedelta(days=base_start.weekday())
    
    # 終了日が日曜になるように調整
    days_to_add = 6 - base_end.weekday()
    end_date = base_end + timedelta(days=days_to_add)

    calendar_weeks = []
    current_week = []
    current = start_date
    
    mapping_dates = list(MappingSession.objects.filter(patient=patient).values_list('date', flat=True))
    treatments_done = {t.date.date(): t for t in TreatmentSession.objects.filter(patient=patient)}
    assessment_events = []  # 評価イベントを別途収集

    while current <= end_date:
        is_hol = is_holiday(current)
        day_info = {
            'date': current,
            'weekday': ["月", "火", "水", "木", "金", "土", "日"][current.weekday()],
            'weekday_num': current.weekday(),
            'events': [],
            'is_weekend': current.weekday() >= 5,
            'is_holiday': is_hol,
            'url': build_url('dashboard', query={'date': current.strftime('%Y-%m-%d')})
        }
        
        # 1. 入院
        if current == patient.admission_date:
            day_info['events'].append({'type': 'admission', 'label': '入院', 'url': build_url('admission_procedure', [patient.id])})
            
        # 2. 位置決め
        if current == patient.mapping_date or current in mapping_dates:
            day_info['events'].append({
                'type': 'mapping',
                'label': '位置決め',
                'url': build_url("mapping_add", args=[patient.id], query={"date": current.strftime("%Y-%m-%d")})
            })
            
        # 3. 治療予定・実績
        session_num = 0
        if treatment_start and is_treatment_day(current):
            session_num = get_session_number(treatment_start, current)
            if session_num > 0 and session_num <= 30:
                status_label = ""
                if current in treatments_done: status_label = " (済)"
                day_info['events'].append({
                    'type': 'treatment',
                    'label': f'治療 {session_num}回{status_label}',
                    'url': build_url('treatment_add', [patient.id], {'date': current})
                })
                
                # 4. 評価予定
                # 治療日に評価を表示するのではなく、期限最終日に表示する
                pass  # 後で全体で処理
        
        # 5. 退院
        if current == patient.discharge_date:
            day_info['events'].append({'type': 'discharge', 'label': '退院準備', 'url': build_url('patient_home', [patient.id])})

        elif not patient.discharge_date and treatment_start:
            if treatment_end_est and current == treatment_end_est + timedelta(days=1):
                day_info['events'].append({'type': 'discharge', 'label': '退院準備（予定）', 'url': build_url('patient_home', [patient.id])})

        current_week.append(day_info)
        
        if current.weekday() == 6:
            calendar_weeks.append(current_week)
            current_week = []
            
        current += timedelta(days=1)
        
    if current_week: calendar_weeks.append(current_week)
    
    # 評価イベントを window_end に追加
    for timing in ['baseline', 'week3', 'week6']:
        ws, we = get_assessment_window(patient, timing)
        if we and start_date <= we <= end_date:
            # 該当日の day_info を探す
            for week in calendar_weeks:
                for day in week:
                    if day['date'] == we:
                        existing = Assessment.objects.filter(patient=patient, timing=timing).exists()
                        label = {
                            'baseline': f'治療前評価（HAM-D） ({we.strftime("%m/%d")})',
                            'week3': f'第3週目評価（HAM-D） ({we.strftime("%m/%d")})',
                            'week6': f'第6週目評価（HAM-D） ({we.strftime("%m/%d")})'
                        }.get(timing, timing)
                        if existing:
                            label += ' (済)'
                        event = {
                            'type': 'assessment',
                            'label': label,
                            'url': build_url('assessment_add', [patient.id, timing], query={'from': 'clinical_path'}),
                            'date': we,
                            'timing': timing,
                            'window_end': we
                        }
                        day['events'].append(event)
                        assessment_events.append(event)
                        break
    
    return calendar_weeks, assessment_events

HAMD_ANCHORS = {
    "q1": "0. なし\n1. 質問をされた時のみ示される（一時的、軽度のうつ状態）\n2. 自ら言葉で訴える（持続的、軽度から中等度のうつ状態）\n3. 言葉を使わなくとも伝わる（例えば、表情・姿勢・声・涙もろさ）（持続的、中等度から重度のうつ状態）\n4. 言語的にも、非言語的にも、事実上こうした気分の状態のみが、自然に表現される（持続的、極めて重度のうつ状態、希望のなさや涙もろさが顕著）",
    "q2": "0. なし\n1. 自己非難、他人をがっかりさせたという思い（生産性の低下に対する自責感のみ）\n2. 過去の過ちや罪深い行為に対する、罪責観念や思考の反復（罪責、後悔、あるいは恥の感情）\n3. 現在の病気は自分への罰であると考える、罪責妄想（重度で広範な罪責感）\n4. 非難や弾劾するような声が聞こえ、そして（あるいは）脅されるような幻視を体験する",
    "q3": "0. なし\n1. 生きる価値がないと感じる\n2. 死ねたらという願望、または自己の死の可能性を考える\n3. 自殺念慮、自殺をほのめかす行動をとる\n4. 自殺を企図する",
    "q4": "0. 入眠困難はない\n1. 時々寝つけない、と訴える（すなわち、30分以上、週に2-3日）\n2. 夜ごと寝つけない、と訴える（すなわち、30分以上、週に4日以上）",
    "q5": "0. 熟眠困難はない\n1. 夜間、睡眠が不安定で、妨げられると訴える（または、時々、すなわち週に2-3日、夜中に30分以上覚醒している）\n2. 夜中に目が覚めてしまう―トイレ以外で、寝床から出てしまういかなる場合も含む（しばしば、すなわち週に4日以上、夜中に30分以上覚醒している）",
    "q6": "0. 早朝睡眠に困難はない\n1. 早朝に目が覚めるが、再び寝つける（時々、すなわち、週に2～3日、早朝に30分以上目が覚める）\n2. 一度起き出すと、再び寝つくことはできない（しばしば、すなわち、週に4日以上、早朝に30分以上目が覚める）",
    "q7": "0. 困難なくできる\n1. 活動、仕事、あるいは趣味に関連して、それができない、疲れる、弱気であるといった思いがある（興味や喜びは軽度減退しているが、機能障害は明らかではない）\n2. 活動・趣味・仕事に対する興味の喪失―患者が直接訴える、あるいは、気乗りのなさ、優柔不断、気迷いから間接的に判断される（仕事や活動をするのに無理せざるを得ないと感じる興味や喜び、機能は明らかに減退している）\n3. 活動に費やす実時間の減少、あるいは生産性の低下（興味や喜び、機能の深刻な減退）\n4. 現在の病気のために、働くことをやめた（病気のために仕事あるいは主要な役割を果たすことができない、そして興味も完全に喪失している）",
    "q8": "0. 発話・思考は正常である\n1. 面接時に軽度の遅滞が認められる（または、軽度の精神運動抑制）\n2. 面接時に明らかな遅滞が認められる（すなわち、中等度、面接はいくらか困難；話は途切れがちで、思考速度は遅い）\n3. 面接は困難である（重度の精神運動抑制、話はかなり長く途切れてしまい、面接は非常に困難）\n4. 完全な昏迷（極めて重度の精神運動抑制：昏迷：面接はほとんど不可能）",
    "q9": "0. なし（正常範囲内の動作）\n1. そわそわする\n2. 手や髪などをいじくる\n3. 動き回る、じっと座っていられない\n4. 手を握りしめる、爪を噛む、髪を引っ張る、唇を噛む（面接は不可能）",
    "q10": "0. 問題なし\n1. 主観的な緊張とイライラ感（軽度、一時的）\n2. 些細な事柄について悩む（中等度、多少の苦痛をもたらす、あるいは実在する問題に過度に悩んでいる）\n3. 心配な態度が顔つきや話し方から明らかである（重度：不安のために機能障害が生じている）\n4. 疑問の余地なく恐怖が表出されている（何もできない程の症状）",
    "q11": "0. なし\n1. 軽度（症状は時々出現するのみ、機能の障害はない。わずかな苦痛）\n2. 中等度（症状はより持続する、普段の活動に多少の支障をきたす、中等度の苦痛）\n3. 重度（顕著な機能の障害）\n4. 何もできなくなる",
    "q12": "0. なし\n1. 食欲はないが、促されなくても食べている（普段より食欲はいくらか低下）\n2. 促されないと食事摂取が困難（あるいは、無理して食べなければならないかどうかに関わらず、食欲は顕著に低下している）",
    "q13": "0. なし\n1. 手足や背中、あるいは頭の重苦しさ。背部痛、頭痛、筋肉痛。元気のなさや易疲労性（普段より気力はいくらか低下：軽度で一時的な、気力の喪失や筋肉の痛み／重苦しさ）\n2. 何らかの明白な症状（持続的で顕著な、気力の喪失や筋肉の痛み／重苦しさ）",
    "q14": "0. なし\n1. 軽度（普段よりいくらか関心が低下）\n2. 重度（普段よりかなり関心が低下）",
    "q15": "0. なし（不適切な心配はない、あるいは完全に安心できる）\n1. 体のことが気がかりである（自分の健康に関する多少の不適切な心配、または大丈夫だと言われているにも関わらず、わずかに心配している）\n2. 健康にこだわっている（しばしば自身の健康に対し過剰に心配する、あるいは医学的に大丈夫だと明言されているにも関わらず、特別な病気があると思い込んでいる）\n3. 訴えや助けを求めること等が頻繁にみられる（医師が確認できていない身体的問題があると確信している：身体的な健康についての誇張された、現実的でない心配）\n4. 心気妄想（例えば、体の一部が衰え、腐ってしまうと感じる、など、外来患者ではまれである）",
    "q16": "現病歴による評価の場合：\n0. 体重減少なし、あるいは今回の病気による減少ではない\n1. 今回のうつ病により、おそらく体重が減少している\n2. （患者によると）うつ病により、明らかに体重が減少している",
    "q17": "0. うつ状態であり病気であることを認める、または現在うつ状態でない\n1. 病気であることを認めるが、原因を粗食、働き過ぎ、ウィルス、休息の必要性などのせいにする（病気を否定するが、病気である可能性は認める、例えば「私はどこも悪いところはないと思います、でも他の人には悪く見えるようです」）\n2. 病気であることを全く認めない（病気であることを完全に否定する、例えば「私はうつ病ではありません、私は元気です」）",
    "q18": "A. 症状が悪化するのは朝方なのか夕方なのかを記録し、日内変動のない場合は「なし」にマークする。\n0. なし\n1. 午前に悪い\n2. 午後に悪い\n\nB. 日内変動がある場合、変動の程度をマークする。\n0. なし\n1. 軽度\n2. 重度",
    "q19": "0. なし\n1. 軽度\n2. 中等度\n3. 重度\n4. 何もできなくなる",
    "q20": "0. なし\n1. 疑念をもっている\n2. 関係念慮\n3. 被害関係妄想",
    "q21": "0. なし\n1. 軽度\n2. 重度",
}

# ==========================================
# ビュー関数
# ==========================================

@login_required
def dashboard_view(request):
    jst_now = timezone.localtime(timezone.now())
    if 'date' not in request.GET: return redirect(f'{request.path}?date={jst_now.strftime("%Y-%m-%d")}')
    try: target_date = parse_date(request.GET.get('date'))
    except: target_date = jst_now.date()
    if not target_date: target_date = jst_now.date()
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    target_date_display = f"{target_date.year}年{target_date.month}月{target_date.day}日 ({weekdays[target_date.weekday()]})"
    prev_day = target_date - timedelta(days=1); next_day = target_date + timedelta(days=1)

    task_first_visit = [{'obj': p, 'status': "診察済", 'todo': "初診"} for p in Patient.objects.filter(created_at__date=target_date)]
    task_admission = []; task_mapping = []; task_treatment = []; task_assessment = []; task_discharge = []

    for p in Patient.objects.filter(admission_date=target_date):
        status = "手続済" if p.is_admission_procedure_done else "要手続"; color = "success" if p.is_admission_procedure_done else "warning"
        task_admission.append({'obj': p, 'status': status, 'color': color, 'todo': "入院手続き"})
    for p in Patient.objects.filter(mapping_date=target_date):
        is_done = MappingSession.objects.filter(patient=p, date=target_date).exists()
        task_mapping.append({'obj': p, 'status': "実施済" if is_done else "実施未", 'color': "success" if is_done else "danger", 'todo': "MT測定"})

    pre_candidates = Patient.objects.filter(admission_date__lte=target_date).filter(Q(first_treatment_date__isnull=True) | Q(first_treatment_date__gte=target_date))
    for p in pre_candidates:
        ws, we = get_assessment_window(p, 'baseline')
        if ws <= target_date <= we:
            done = Assessment.objects.filter(patient=p, timing='baseline').exists()
            if not done: task_assessment.append({'obj': p, 'status': "実施未", 'color': "danger", 'timing_code': 'baseline', 'todo': f"治療前評価 ({we.strftime('%m/%d')})"})
            elif Assessment.objects.filter(patient=p, timing='baseline', date=target_date).exists(): task_assessment.append({'obj': p, 'status': "実施済", 'color': "success", 'timing_code': 'baseline', 'todo': "治療前評価 (完了)"})

    active_candidates = Patient.objects.filter(first_treatment_date__lte=target_date).order_by('card_id')
    for p in active_candidates:
        week_num = get_current_week_number(p.first_treatment_date, target_date); session_count_so_far = get_session_count(p, target_date)
        
        if is_treatment_day(target_date) and session_count_so_far < 30:
            today_session = TreatmentSession.objects.filter(patient=p, date__date=target_date).first(); is_done = today_session is not None
            current_count = session_count_so_far if is_done else session_count_so_far + 1
            task_treatment.append({'obj': p, 'note': f"第{week_num}週 ({current_count}回目)", 'status': "実施済" if is_done else "実施未", 'color': "success" if is_done else "danger", 'session_num': current_count, 'todo': "rTMS治療"})
        
        target_timing = None; todo_label = ""
        if week_num == 3: target_timing = 'week3'; todo_label = "第3週目評価"
        elif week_num == 6: target_timing = 'week6'; todo_label = "第6週目評価"
        if target_timing:
            ws, we = get_assessment_window(p, target_timing)
            if ws <= target_date <= we:
                assessment = Assessment.objects.filter(patient=p, timing=target_timing, date__range=[ws, we]).first()
                if assessment:
                    if assessment.date == target_date: task_assessment.append({'obj': p, 'status': "実施済", 'color': "success", 'timing_code': target_timing, 'todo': f"{todo_label} (完了)"})
                else: task_assessment.append({'obj': p, 'status': "実施未", 'color': "danger", 'timing_code': target_timing, 'todo': f"{todo_label} ({we.strftime('%m/%d')})"})
        if session_count_so_far == 30: task_discharge.append({'obj': p, 'status': "退院準備", 'color': "info", 'todo': "サマリー・紹介状作成"})

    # 退院準備: 退院日が確定している患者
    discharge_patients = Patient.objects.filter(discharge_date=target_date)
    for p in discharge_patients:
        task_discharge.append({'obj': p, 'status': "退院準備", 'color': "info", 'todo': "サマリー・紹介状作成"})

    # 退院準備: 退院日未設定だが推定退院日+1日の患者
    for p in active_candidates:
        if p.discharge_date: continue  # 既に上記で追加済み
        treatment_end_est = get_completion_date(p.first_treatment_date)
        if treatment_end_est and target_date == treatment_end_est + timedelta(days=1):
            task_discharge.append({'obj': p, 'status': "退院準備（予定）", 'color': "info", 'todo': "サマリー・紹介状作成"})

    dashboard_tasks = [{'list': task_first_visit, 'title': "① 初診", 'color': "bg-g-1", 'icon': "fa-user-plus"}, {'list': task_admission, 'title': "② 入院", 'color': "bg-g-2", 'icon': "fa-procedures"}, {'list': task_mapping, 'title': "③ 位置決め", 'color': "bg-g-3", 'icon': "fa-crosshairs"}, {'list': task_treatment, 'title': "④ 治療実施", 'color': "bg-g-4", 'icon': "fa-bolt"}, {'list': task_assessment, 'title': "⑤ 尺度評価", 'color': "bg-g-5", 'icon': "fa-clipboard-check"}, {'list': task_discharge, 'title': "⑥ 退院準備", 'color': "bg-g-6", 'icon': "fa-file-export"}]
    return render(request, 'rtms_app/dashboard.html', {'today': target_date, 'target_date_display': target_date_display, 'prev_day': prev_day, 'next_day': next_day, 'today_raw': jst_now.date(), 'dashboard_tasks': dashboard_tasks})

@login_required
def patient_list_view(request):
    dashboard_date = request.GET.get('dashboard_date')

    # ===== 検索/フィルタ（追加） =====
    q = (request.GET.get('q') or '').strip()
    card = (request.GET.get('card') or '').strip()
    status = (request.GET.get('status') or '').strip()  # waiting/inpatient/discharged

    sort_param = request.GET.get('sort', 'card_id')
    dir_param = request.GET.get('dir', 'asc')
    direction = 'desc' if dir_param == 'desc' else 'asc'

    sort_fields = {
        'card_id': ['card_id'],
        'name': ['name'],
        'birth_date': ['birth_date'],
        'gender': ['gender'],
        'attending': ['attending_physician__last_name', 'attending_physician__first_name'],
        'course': ['course_number'],
        'age': ['birth_date'],
        # 追加してよければ：状態でもソート可能
        'status': ['status'],
    }

    if sort_param not in sort_fields:
        sort_param = 'card_id'
        direction = 'asc'

    def build_ordering(key: str, dir_value: str):
        if key == 'age':
            # 年齢昇順（若い→）＝ birth_date 降順
            base_fields = ['-birth_date'] if dir_value == 'asc' else ['birth_date']
        else:
            base_fields = [
                f"-{field}" if dir_value == 'desc' else field
                for field in sort_fields.get(key, ['card_id'])
            ]
        return [*base_fields, 'id']

    ordering = build_ordering(sort_param, direction)

    # ===== QuerySet（ここがポイント） =====
    qs = Patient.objects.select_related('attending_physician').all()

    if q:
        qs = qs.filter(name__icontains=q)

    if card:
        qs = qs.filter(card_id__icontains=card)

    if status:
        # 予期しない値は無視（安全）
        if status in {'waiting', 'inpatient', 'discharged'}:
            qs = qs.filter(status=status)

    patients = qs.order_by(*ordering)

    # ===== sort link 用：検索条件を保持 =====
    preserved_params = request.GET.copy()
    preserved_params.pop('page', None)

    def build_sort_query(target_key: str):
        params = preserved_params.copy()
        params['sort'] = target_key
        params['dir'] = 'desc' if (sort_param == target_key and direction == 'asc') else 'asc'
        return params.urlencode()

    sort_queries = {key: build_sort_query(key) for key in sort_fields.keys()}

    context = {
        'patients': patients,
        'dashboard_date': dashboard_date,
        'current_sort': sort_param,
        'current_dir': direction,
        'sort_queries': sort_queries,

        # フォームに値を戻す
        'q': q,
        'card': card,
        'status': status,
    }

    return render(request, 'rtms_app/patient_list.html', context)

@login_required
def admission_procedure(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id); dashboard_date = request.GET.get('dashboard_date')
    if request.method == 'POST':
        form = AdmissionProcedureForm(request.POST, instance=patient)
        if form.is_valid(): proc = form.save(commit=False); proc.is_admission_procedure_done = True; proc.save(); return redirect(f"/app/dashboard/?date={dashboard_date}" if dashboard_date else 'rtms_app:dashboard')
    else: form = AdmissionProcedureForm(instance=patient)
    return render(request, 'rtms_app/admission_procedure.html', {'patient': patient, 'form': form, 'dashboard_date': dashboard_date})

@login_required
def mapping_add(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id); dashboard_date = request.GET.get('dashboard_date')
    history = MappingSession.objects.filter(patient=patient).order_by('date')
    if request.method == 'POST':
        form = MappingForm(request.POST)
        if form.is_valid(): m = form.save(commit=False); m.patient = patient; m.save(); return redirect(f"/app/dashboard/?date={dashboard_date}" if dashboard_date else 'rtms_app:dashboard')
    else: initial_date = request.GET.get('date') or timezone.now().date(); form = MappingForm(initial={'date': initial_date, 'week_number': 1})
    return render(request, 'rtms_app/mapping_add.html', {'patient': patient, 'form': form, 'history': history, 'dashboard_date': dashboard_date})

@login_required
def patient_first_visit(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id); dashboard_date = request.GET.get('dashboard_date')
    all_patients = Patient.objects.all(); referral_map = {}; referral_sources_set = set()
    for p in all_patients:
        if p.referral_source: referral_sources_set.add(p.referral_source); 
        if p.referral_doctor:
            if p.referral_source not in referral_map: referral_map[p.referral_source] = set()
            referral_map[p.referral_source].add(p.referral_doctor)
    referral_map_json = {k: sorted(list(v)) for k, v in referral_map.items()}; referral_options = sorted(list(referral_sources_set))
    end_date_est = get_completion_date(patient.first_treatment_date)
    hamd_items = [
    ('q1', '1. 抑うつ気分', 4, HAMD_ANCHORS['q1']),
    ('q2', '2. 罪責感', 4, HAMD_ANCHORS['q2']),
    ('q3', '3. 自殺', 4, HAMD_ANCHORS['q3']),
    ('q4', '4. 入眠障害', 2, HAMD_ANCHORS['q4']),
    ('q5', '5. 熟眠障害', 2, HAMD_ANCHORS['q5']),
    ('q6', '6. 早朝睡眠障害', 2, HAMD_ANCHORS['q6']),
    ('q7', '7. 仕事と活動', 4, HAMD_ANCHORS['q7']),
    ('q8', '8. 精神運動抑制', 4, HAMD_ANCHORS['q8']),
    ('q9', '9. 精神運動激越', 4, HAMD_ANCHORS['q9']),
    ('q10', '10. 不安, 精神症状', 4, HAMD_ANCHORS['q10']),
    ('q11', '11. 不安, 身体症状', 4, HAMD_ANCHORS['q11']),
    ('q12', '12. 身体症状, 消化器系', 2, HAMD_ANCHORS['q12']),
    ('q13', '13. 身体症状, 一般的', 2, HAMD_ANCHORS['q13']),
    ('q14', '14. 生殖器症状', 2, HAMD_ANCHORS['q14']),
    ('q15', '15. 心気症', 4, HAMD_ANCHORS['q15']),
    ('q16', '16. この1週間の体重減少', 2, HAMD_ANCHORS['q16']),
    ('q17', '17. 病識', 2, HAMD_ANCHORS['q17']),
    ('q18', '18. 日内変動', 2, HAMD_ANCHORS['q18']),
    ('q19', '19. 現実感喪失, 離人症', 4, HAMD_ANCHORS['q19']),
    ('q20', '20. 妄想症状', 3, HAMD_ANCHORS['q20']),
    ('q21', '21. 強迫症状', 2, HAMD_ANCHORS['q21']),
    ]
    hamd_items_left = hamd_items[:11]; hamd_items_right = hamd_items[11:]
    baseline_assessment = Assessment.objects.filter(patient=patient, timing='baseline').first()

    if request.method == 'POST':
        if 'hamd_ajax' in request.POST:
            try:
                scores = {}
                for key, _, _, _ in hamd_items: scores[key] = int(request.POST.get(key, 0))
                note = request.POST.get('hamd_note', '').strip()
                if baseline_assessment: assessment = baseline_assessment; assessment.scores = scores; assessment.note = note
                else: assessment = Assessment(patient=patient, date=timezone.now().date(), type='HAM-D', scores=scores, timing='baseline', note=note)
                assessment.calculate_scores(); assessment.save()
                total = assessment.total_score_17; msg = ""; severity = ""
                if 14 <= total <= 18: severity = "中等症"; msg = "中等症と判定しました。rTMS適正質問票を確認してください。"
                elif total >= 19: severity = "重症"; msg = "重症と判定しました。"
                elif 8 <= total <= 13: severity = "軽症"
                else: severity = "正常"
                return JsonResponse({'status': 'success', 'total_17': total, 'severity': severity, 'message': msg})
            except Exception as e: return JsonResponse({'status': 'error', 'message': str(e)})
        form = PatientFirstVisitForm(request.POST, instance=patient)
        if form.is_valid():
            p = form.save(commit=False); diag_list = request.POST.getlist('diag_list'); diag_other = request.POST.get('diag_other', '').strip()
            full_diagnosis = ", ".join(diag_list);
            if diag_other: full_diagnosis += f", その他({diag_other})"
            p.diagnosis = full_diagnosis; p.save()

            action = request.POST.get('action')

            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                redirect_url = f"{reverse('rtms_app:dashboard')}?date={dashboard_date}" if dashboard_date else reverse('rtms_app:dashboard')
                return JsonResponse({'status': 'success', 'redirect_url': redirect_url})

            if action == 'print_bundle':
                query = {'docs': ['admission', 'suitability', 'consent_pdf']}
                if dashboard_date:
                    query['dashboard_date'] = dashboard_date
                return redirect(build_url('patient_print_bundle', args=[patient.id], query=query))

            if dashboard_date:
                return redirect(f"{reverse('rtms_app:dashboard')}?date={dashboard_date}")
            return redirect('rtms_app:dashboard')
    else: form = PatientFirstVisitForm(instance=patient)
    floating_print_options = [{
        'label': '印刷プレビュー',
        'value': 'print_bundle',
        'icon': 'fa-print',
        'formaction': reverse('rtms_app:patient_print_bundle', args=[patient.id]),
        'formtarget': '_blank',
        'docs_form_id': 'bundlePrintFormFirstVisit',
    }]
    return render(request, 'rtms_app/patient_first_visit.html', {'patient': patient, 'form': form, 'referral_options': referral_options, 'referral_map_json': json.dumps(referral_map_json, ensure_ascii=False), 'end_date_est': end_date_est, 'dashboard_date': dashboard_date, 'hamd_items_left': hamd_items_left, 'hamd_items_right': hamd_items_right, 'baseline_assessment': baseline_assessment, 'floating_print_options': floating_print_options, 'can_view_audit': can_view_audit(request.user)})

@login_required
def treatment_add(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id); dashboard_date = request.GET.get('dashboard_date')
    latest_mapping = MappingSession.objects.filter(patient=patient).order_by('-date').first()
    side_effect_items = [('headache', '頭痛'), ('scalp', '頭皮痛（刺激痛）'), ('discomfort', '刺激部位の不快感'), ('tooth', '歯痛'), ('twitch', '顔面のけいれん'), ('dizzy', 'めまい'), ('nausea', '吐き気'), ('tinnitus', '耳鳴り'), ('hearing', '聴力低下'), ('anxiety', '不安感・焦燥感'), ('other', 'その他')]
    target_date_str = request.GET.get('date'); now = timezone.localtime(timezone.now())
    if target_date_str: t = parse_date(target_date_str); initial_date = t
    else: initial_date = now.date()
    session_num = get_session_count(patient, initial_date) + 1
    week_num = get_current_week_number(patient.first_treatment_date, initial_date); end_date_est = get_completion_date(patient.first_treatment_date)
    alert_msg = ""; instruction_msg = ""; is_remission = False
    last_assessment = Assessment.objects.filter(patient=patient, timing='week3').order_by('-date').first(); baseline_assessment = Assessment.objects.filter(patient=patient, timing='baseline').order_by('-date').first(); judgment_info = None
    if last_assessment:
        score_now = last_assessment.total_score_17
        if score_now <= 7:
            is_remission = True; judgment_info = f"寛解 (HAM-D17: {score_now}点)"; instruction_msg = "【指示】第4週以降は漸減プロトコルに従ってください。"
        else:
            if baseline_assessment and baseline_assessment.total_score_17 > 0:
                imp_rate = (baseline_assessment.total_score_17 - score_now) / baseline_assessment.total_score_17
                if imp_rate >= 0.2: judgment_info = f"有効 (改善率 {int(imp_rate*100)}%)"; instruction_msg = "【指示】有効性あり。治療を継続してください。"
                else: judgment_info = f"無効/反応不良 (改善率 {int(imp_rate*100)}%)"; instruction_msg = "【指示】治療未反応。続行または中止を検討してください。"
            else: judgment_info = f"判定不能 (Baseデータなし)"
        if is_remission and week_num >= 4:
            weekly_count = get_weekly_session_count(patient, initial_date); current_weekly = weekly_count + 1
            if week_num == 4:
                if current_weekly > 3: alert_msg = f"【制限超過】第4週(週3回まで)です。今回で週{current_weekly}回目になります。"
                else: alert_msg = f"【漸減】第4週です。週3回まで (現在: 週{current_weekly}回目)"
            elif week_num == 5:
                if current_weekly > 2: alert_msg = f"【制限超過】第5週(週2回まで)です。今回で週{current_weekly}回目になります。"
                else: alert_msg = f"【漸減】第5週です。週2回まで (現在: 週{current_weekly}回目)"
            elif week_num == 6:
                if current_weekly > 1: alert_msg = f"【制限超過】第6週(週1回まで)です。今回で週{current_weekly}回目になります。"
                else: alert_msg = f"【漸減】第6週です。週1回まで (現在: 週{current_weekly}回目)"
            elif week_num >= 7: alert_msg = "【警告】第7週以降のため、原則として治療は算定できません。"
    if request.method == 'POST':
        form = TreatmentForm(request.POST)
        if form.is_valid():
            s = form.save(commit=False); s.patient = patient; s.performer = request.user
            d = form.cleaned_data['treatment_date']; t = form.cleaned_data['treatment_time']; dt = datetime.datetime.combine(d, t); s.date = timezone.make_aware(dt)
            se_data = {}; 
            for key, label in side_effect_items: val = request.POST.get(f'se_{key}'); 
            if val: se_data[key] = val
            se_data['note'] = request.POST.get('se_note', ''); s.side_effects = se_data; s.save()
            # AJAX (autosave) 対応: JSON を返す
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                redirect_url = f"/app/dashboard/?date={dashboard_date}" if dashboard_date else reverse('rtms_app:dashboard')
                return JsonResponse({'status': 'success', 'id': s.id, 'redirect_url': redirect_url})
            return redirect(f"/app/dashboard/?date={dashboard_date}" if dashboard_date else 'rtms_app:dashboard')
        else:
            # バリデーション失敗時の Ajax 応答
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'status': 'error', 'errors': form.errors}, status=400)
    else:
        initial_data = {'treatment_date': initial_date, 'treatment_time': now.strftime('%H:%M'), 'total_pulses': 1980, 'intensity': 120}
        if latest_mapping: initial_data['motor_threshold'] = latest_mapping.resting_mt
        form = TreatmentForm(initial=initial_data)
    return render(request, 'rtms_app/treatment_add.html', {'patient': patient, 'form': form, 'latest_mapping': latest_mapping, 'side_effect_items': side_effect_items, 'session_num': session_num, 'week_num': week_num, 'end_date_est': end_date_est, 'start_date': patient.first_treatment_date, 'dashboard_date': dashboard_date, 'alert_msg': alert_msg, 'instruction_msg': instruction_msg, 'judgment_info': judgment_info})

@login_required
def assessment_add(request, patient_id, timing):
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get("dashboard_date")
    from_page = request.GET.get("from")

    # Validate timing against model choices to prevent tampering
    allowed = [c[0] for c in Assessment.TIMING_CHOICES]
    if timing not in allowed:
        return HttpResponse(status=400)

    # ★ 期限超過による強制終了はしない（要件変更）
    # （ここにあった baseline の HttpResponse ブロックは削除）

    history = Assessment.objects.filter(patient=patient).order_by("date")

    hamd_items = [
        ("q1", "1. 抑うつ気分", 4, HAMD_ANCHORS["q1"]),
        ("q2", "2. 罪責感", 4, HAMD_ANCHORS["q2"]),
        ("q3", "3. 自殺", 4, HAMD_ANCHORS["q3"]),
        ("q4", "4. 入眠障害", 2, HAMD_ANCHORS["q4"]),
        ("q5", "5. 熟眠障害", 2, HAMD_ANCHORS["q5"]),
        ("q6", "6. 早朝睡眠障害", 2, HAMD_ANCHORS["q6"]),
        ("q7", "7. 仕事と活動", 4, HAMD_ANCHORS["q7"]),
        ("q8", "8. 精神運動抑制", 4, HAMD_ANCHORS["q8"]),
        ("q9", "9. 精神運動激越", 4, HAMD_ANCHORS["q9"]),
        ("q10", "10. 不安, 精神症状", 4, HAMD_ANCHORS["q10"]),
        ("q11", "11. 不安, 身体症状", 4, HAMD_ANCHORS["q11"]),
        ("q12", "12. 身体症状, 消化器系", 2, HAMD_ANCHORS["q12"]),
        ("q13", "13. 身体症状, 一般的", 2, HAMD_ANCHORS["q13"]),
        ("q14", "14. 生殖器症状", 2, HAMD_ANCHORS["q14"]),
        ("q15", "15. 心気症", 4, HAMD_ANCHORS["q15"]),
        ("q16", "16. この1週間の体重減少", 2, HAMD_ANCHORS["q16"]),
        ("q17", "17. 病識", 2, HAMD_ANCHORS["q17"]),
        ("q18", "18. 日内変動", 2, HAMD_ANCHORS["q18"]),
        ("q19", "19. 現実感喪失, 離人症", 4, HAMD_ANCHORS["q19"]),
        ("q20", "20. 妄想症状", 3, HAMD_ANCHORS["q20"]),
        ("q21", "21. 強迫症状", 2, HAMD_ANCHORS["q21"]),
    ]
    mid_index = 11
    hamd_items_left = hamd_items[:mid_index]
    hamd_items_right = hamd_items[mid_index:]

    # 表示上の「実施日」初期値：GET date があればそれ、なければ今日
    target_date_str = request.GET.get("date")
    target_date = parse_date(target_date_str) if target_date_str else None
    if not target_date:
        target_date = timezone.localdate()

    # ★ window 計算（表示＆検索に使う）
    window_start, window_end = get_assessment_window(patient, timing)

    # ★ 既存評価の取得方針
    # baseline: timing='baseline' の最新を編集対象
    # week3/week6: window 内の最新を編集対象（date文字列一致は廃止）
    if timing == "baseline":
        existing_assessment = (
            Assessment.objects.filter(patient=patient, type="HAM-D", timing="baseline")
            .order_by("-date")
            .first()
        )
    elif timing in ("week3", "week6"):
        existing_assessment = (
            Assessment.objects.filter(
                patient=patient,
                type="HAM-D",
                timing=timing,
                date__range=[window_start, window_end],
            )
            .order_by("-date")
            .first()
        )
    else:
        existing_assessment = None

    # baseline を常に取得してテンプレートに渡す（改善率計算などに使用）
    baseline = (
        Assessment.objects.filter(patient=patient, timing="baseline", type="HAM-D")
        .order_by("-date")
        .first()
    )
    baseline_total = baseline.total_score_21 if baseline else None
    baseline_scores = baseline.scores if baseline else None

    # 参考：week3/week6 で既存がある場合だけ recommendation を出す（必要なら今後拡張）
    recommendation = ""
    if timing in ("week3", "week6") and baseline and baseline.total_score_21 > 0 and existing_assessment:
        imp = (baseline.total_score_21 - existing_assessment.total_score_21) / baseline.total_score_21 * 100
        if imp < 20: recommendation = "【判定】反応不良 (改善率20%未満)。刺激部位やプロトコルの変更を検討してください。"
        else: recommendation = "【判定】治療継続 (順調に改善中)。"

    if request.method == "POST":
        try:
            scores = {}
            for key, label, mx, text in hamd_items:
                scores[key] = int(request.POST.get(key, 0))

            note = request.POST.get("note", "")

            if existing_assessment:
                assessment = existing_assessment
                assessment.scores = scores
                assessment.note = note
                # baselineは既存のdate維持、week3/week6は入力日として target_date を採用
                if timing in ("week3", "week6"):
                    assessment.date = target_date
                assessment.timing = timing
                assessment.type = "HAM-D"
            else:
                assessment = Assessment(
                    patient=patient,
                    date=target_date,
                    type="HAM-D",
                    scores=scores,
                    timing=timing,
                    note=note,
                )

            assessment.calculate_scores()
            assessment.save()

            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse(
                    {
                        "status": "success",
                        "id": assessment.id,
                        "total_17": assessment.total_score_17,
                        "total_21": assessment.total_score_21,
                    }
                )

            # クリニカルパスから来た場合は戻す
            if from_page == "clinical_path":
                url = reverse("rtms_app:patient_clinical_path", args=[patient.id])
                if dashboard_date:
                    url += f"?dashboard_date={dashboard_date}"
                url += f"&focus={assessment.date.strftime('%Y-%m-%d')}"
                return redirect(url)

            return redirect(f"/app/dashboard/?date={dashboard_date}" if dashboard_date else "rtms_app:dashboard")

        except Exception as e:
            import traceback
            traceback.print_exc()
            if request.headers.get("x-requested-with") == "XMLHttpRequest":
                return JsonResponse({"status": "error", "message": str(e)}, status=500)

    timing_display = dict(Assessment.TIMING_CHOICES).get(timing, timing)

    context = {
        "patient": patient,
        "history": history,
        "today": target_date,  # Date型で渡す
        "hamd_items_left": hamd_items_left,
        "hamd_items_right": hamd_items_right,
        "initial_timing": timing,
        "initial_timing_display": timing_display,
        "existing_assessment": existing_assessment,
        "recommendation": recommendation,
        "dashboard_date": dashboard_date,

        # ★ 期限ではなく window 表示
        "window_start": window_start,
        "window_end": window_end,

        "baseline_total": baseline_total,
        "baseline_scores": baseline_scores,
    }
    return render(request, "rtms_app/assessment_add.html", context)

@login_required
def patient_summary_view(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date')

    if request.method == 'POST':
        patient.summary_text = request.POST.get('summary_text', '')
        patient.discharge_prescription = request.POST.get('discharge_prescription', '')

        d_date = request.POST.get('discharge_date')
        if d_date:
            patient.discharge_date = parse_date(d_date)
        else:
            patient.discharge_date = None

        patient.save()

        action = request.POST.get('action')

        # ★ AJAXの場合でも action を見て印刷URLを返す
        if request.headers.get('x-requested-with') == 'XMLHttpRequest':
            if action == 'print_bundle':
                return JsonResponse({'status': 'success'})
            if action == 'print_discharge':
                return JsonResponse({
                    'status': 'success',
                    'redirect_url': reverse("rtms_app:patient_print_discharge", args=[patient.id]),
                })
            if action == 'print_referral':
                return JsonResponse({
                    'status': 'success',
                    'redirect_url': reverse("rtms_app:patient_print_referral", args=[patient.id]),
                })
            else:
                redirect_url = f"{reverse('rtms_app:dashboard')}?date={dashboard_date}" if dashboard_date else reverse('rtms_app:dashboard')
                return JsonResponse({'status': 'success', 'redirect_url': redirect_url})

        # ★ 通常POST（非AJAX）
        if action == 'print_bundle':
            return redirect(
                build_url(
                    'patient_print_bundle',
                    args=[patient.id],
                    query={'docs': ['discharge', 'referral']},
                )
            )
        if action == 'print_discharge':
            return redirect(reverse("rtms_app:patient_print_discharge", args=[patient.id]))
        if action == 'print_referral':
            return redirect(reverse("rtms_app:patient_print_referral", args=[patient.id]))

        return redirect(f"/app/dashboard/?date={dashboard_date}" if dashboard_date else 'rtms_app:dashboard')

        
    sessions = TreatmentSession.objects.filter(patient=patient).order_by('date'); assessments = Assessment.objects.filter(patient=patient).order_by('date')
    test_scores = assessments; score_admin = assessments.first(); score_w3 = assessments.filter(timing='week3').first(); score_w6 = assessments.filter(timing='week6').first()
    def fmt_score(obj): return f"HAMD17 {obj.total_score_17}点 HAMD21 {obj.total_score_21}点" if obj else "未評価"
    side_effects_list_all = []; history_list = []
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
    floating_print_options = [
        {
            "label": "印刷プレビュー",
            "value": "print_bundle",
            "icon": "fa-print",
            "formaction": reverse("rtms_app:patient_print_bundle", args=[patient.id]),
            "formtarget": "_blank",
            "docs_form_id": "bundlePrintFormDischarge",
        },
    ]
    return render(request, 'rtms_app/patient_summary.html', {'patient': patient, 'summary_text': summary_text, 'history_list': history_list, 'today': timezone.now().date(), 'test_scores': test_scores, 'dashboard_date': dashboard_date, 'floating_print_options': floating_print_options, 'can_view_audit': can_view_audit(request.user)})
    
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
            return redirect('rtms_app:dashboard')
        if existing_patients.exists():
            latest = existing_patients.first()
            return render(request, 'rtms_app/patient_add.html', {'form': form, 'referral_options': referral_options, 'existing_patient': latest, 'next_course_num': latest.course_number + 1})
        if form.is_valid(): form.save(); return redirect('rtms_app:dashboard')
    else: form = PatientRegistrationForm()
    return render(request, 'rtms_app/patient_add.html', {'form': form, 'referral_options': referral_options})

@login_required
def export_treatment_csv(request):
    response = HttpResponse(content_type='text/csv; charset=utf-8-sig'); response['Content-Disposition'] = 'attachment; filename="treatment_data.csv"'; writer = csv.writer(response); writer.writerow(['ID', '氏名', '実施日時', 'MT(%)', '強度(%)', 'パルス数', '実施者', '副作用'])
    treatments = TreatmentSession.objects.all().select_related('patient', 'performer').order_by('date')
    rows = treatments.count()
    for t in treatments: se_str = json.dumps(t.side_effects, ensure_ascii=False) if t.side_effects else ""; writer.writerow([t.patient.card_id, t.patient.name, t.date.strftime('%Y-%m-%d %H:%M'), t.motor_threshold, t.intensity, t.total_pulses, t.performer.username if t.performer else "", se_str])
    meta = {
        'export_type': 'csv',
        'filters': {},
        'rows': rows,
    }
    log_audit_action(None, 'EXPORT', 'TreatmentSession', '', '治療データCSVエクスポート', meta)
    return response

@login_required
def download_db(request):
    if not request.user.is_staff: return HttpResponse("Forbidden", 403)
    db_path = settings.DATABASES['default']['NAME']
    if os.path.exists(db_path): return FileResponse(open(db_path, 'rb'), as_attachment=True, filename='db.sqlite3')
    return HttpResponse("Not found", 404)

def custom_logout(request):
    logout(request)
    return redirect("rtms_app:dashboard")

def patient_print_preview(request, pk):
    patient = get_object_or_404(Patient, pk=pk)
    mode = request.GET.get('mode', 'summary')
    return_to = request.GET.get("return_to") or request.META.get("HTTP_REFERER")

    doc_map = {
        "summary": "admission",
        "questionnaire": "suitability",
    }
    target_doc = doc_map.get(mode, "admission")
    query = {"docs": [target_doc]}
    if return_to:
        query["return_to"] = return_to
    return redirect(build_url("patient_print_bundle", args=[patient.id], query=query))

def _render_patient_summary(request, patient, mode):
    normalized_mode = 'discharge' if mode == 'summary' else mode
    query = {"docs": [normalized_mode]}
    return_to = request.GET.get("return_to") or request.META.get("HTTP_REFERER")
    if return_to:
        query["return_to"] = return_to
    return redirect(build_url("patient_print_bundle", args=[patient.id], query=query))


def patient_print_summary(request, pk):
    patient = get_object_or_404(Patient, pk=pk)
    mode = request.GET.get('mode', 'discharge')
    return _render_patient_summary(request, patient, mode)

@login_required
def print_clinical_path(request, patient_id: int):
    patient = get_object_or_404(Patient, id=patient_id)
    calendar_weeks, assessment_events = generate_calendar_weeks(patient)
    return_to = request.GET.get("return_to") or request.META.get("HTTP_REFERER")
    back_url = return_to or reverse("rtms_app:patient_clinical_path", args=[patient.id])
    return render(request, "rtms_app/print/path.html", {
        "patient": patient,
        "calendar_weeks": calendar_weeks,
        "assessment_events": assessment_events,
        "back_url": back_url,
    })

@login_required
def patient_print_discharge(request, patient_id):
    patient = get_object_or_404(Patient, id=patient_id)
    return_to = request.GET.get("return_to") or request.META.get("HTTP_REFERER")
    return redirect(
        build_url(
            'patient_print_bundle',
            args=[patient.id],
            query={'docs': ['discharge'], 'return_to': return_to} if return_to else {'docs': ['discharge']},
        )
    )


@login_required
def patient_print_referral(request, patient_id):
    patient = get_object_or_404(Patient, id=patient_id)
    return_to = request.GET.get("return_to") or request.META.get("HTTP_REFERER")
    return redirect(
        build_url(
            'patient_print_bundle',
            args=[patient.id],
            query={'docs': ['referral'], 'return_to': return_to} if return_to else {'docs': ['referral']},
        )
    )


@login_required
def consent_latest(request):
    return render(request, "rtms_app/consent_latest.html")


@login_required
def patient_print_bundle(request, patient_id):
    patient = get_object_or_404(Patient, id=patient_id)

    return_to = request.GET.get("return_to") or request.META.get("HTTP_REFERER")

    raw_docs = request.GET.getlist("docs")
    if not raw_docs:
        legacy_docs = request.GET.get("docs")
        if legacy_docs:
            raw_docs = legacy_docs.split(",")

    legacy_map = {
        "consent": "consent_pdf",
    }
    raw_docs = [legacy_map.get(doc, doc) for doc in raw_docs]

    DOC_DEFINITIONS = {
        "admission": {
            "label": "初診時サマリー",
            "template": "rtms_app/print/admission_summary.html",
        },
        "suitability": {
            "label": "rTMS問診票",
            "template": "rtms_app/print/suitability_questionnaire.html",
        },
        "consent_pdf": {
            "label": "説明同意書（PDF）",
            "pdf_static": "rtms_app/docs/rtms_consent_latest.pdf",
        },
        "discharge": {
            "label": "退院時サマリー",
            "template": "rtms_app/print/discharge_summary.html",
        },
        "referral": {
            "label": "紹介状",
            "template": "rtms_app/print/referral.html",
        },
    }
    DOC_ORDER = ["admission", "suitability", "consent_pdf", "discharge", "referral"]

    selected_doc_keys = [d for d in DOC_ORDER if d in raw_docs]

    assessments = Assessment.objects.filter(
        patient=patient
    ).order_by("date")

    end_date_est = get_completion_date(patient.first_treatment_date)
    today = timezone.now().date()
    back_url = return_to or reverse("rtms_app:patient_first_visit", args=[patient.id])

    docs_to_render = []
    for key in selected_doc_keys:
        if key not in DOC_DEFINITIONS:
            continue
        doc_info = DOC_DEFINITIONS[key].copy()
        doc_info["key"] = key
        docs_to_render.append(doc_info)

    context = {
        "patient": patient,
        "docs_to_render": docs_to_render,
        "doc_definitions": DOC_DEFINITIONS,
        "selected_doc_keys": selected_doc_keys,
        "assessments": assessments,
        "test_scores": assessments,
        "consent_copies": ["患者控え", "病院控え"],
        "end_date_est": end_date_est,
        "today": today,
        "back_url": back_url,
    }

    # 印刷ログ記録
    for doc_key in selected_doc_keys:
        doc_label = DOC_DEFINITIONS.get(doc_key, {}).get('label', doc_key)
        meta = {
            'docs': selected_doc_keys,
            'querystring': request.GET.urlencode(),
            'return_to': return_to,
        }
        log_audit_action(patient, 'PRINT', 'Document', doc_key, f'{doc_label}印刷', meta)

    return render(
        request,
        "rtms_app/print/bundle.html",
        context,
    )

@login_required
def patient_clinical_path(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date')
    # ★修正: generate_calendar_weeks を使用
    calendar_weeks, assessment_events = generate_calendar_weeks(patient)
    last_assessment = Assessment.objects.filter(patient=patient, timing='week3').order_by('-date').first()
    baseline_assessment = Assessment.objects.filter(patient=patient, timing='baseline').order_by('-date').first()
    week6_assessment = Assessment.objects.filter(patient=patient, timing='week6').order_by('-date').first()
    floating_print_options = [{
        'label': '印刷プレビュー',
        'icon': 'fa-print',
        'value': 'print_path',
        'formaction': reverse('rtms_app:print_clinical_path', args=[patient.id]),
        'formmethod': 'get',
        'formtarget': '_blank'
    }]
    return render(request, 'rtms_app/patient_clinical_path.html', {
        'patient': patient,
        'calendar_weeks': calendar_weeks,
        'assessment_events': assessment_events,
        'last_assessment': last_assessment,
        'baseline_assessment': baseline_assessment,
        'week6_assessment': week6_assessment,
        'today': timezone.now().date(),
        'dashboard_date': dashboard_date,
        'floating_print_options': floating_print_options,
        'can_view_audit': can_view_audit(request.user)
    })

@login_required
def patient_print_path(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    # ★修正: generate_calendar_weeks を使用
    calendar_weeks, assessment_events = generate_calendar_weeks(patient)
    return_to = request.GET.get("return_to") or request.META.get("HTTP_REFERER")
    back_url = return_to or reverse("rtms_app:patient_clinical_path", args=[patient.id])
    log_audit_action(patient, 'PRINT', 'ClinicalPath', '', '臨床経過表印刷', {
        'docs': ['path'],
        'querystring': request.GET.urlencode(),
        'return_to': return_to,
    })
    return render(request, 'rtms_app/print/path.html', {
        'patient': patient,
        'calendar_weeks': calendar_weeks,
        'assessment_events': assessment_events,
        'back_url': back_url,
    })

@login_required
def audit_logs_view(request, patient_id):
    # 権限チェック: adminユーザーまたはofficeグループ
    if not can_view_audit(request.user):
        return HttpResponse("アクセス権限がありません。", status=403)
    
    patient = get_object_or_404(Patient, pk=patient_id)
    logs = AuditLog.objects.filter(patient=patient).order_by('-created_at')
    
    return render(request, 'rtms_app/audit_logs.html', {
        'patient': patient,
        'logs': logs,
    })

@login_required
def latest_consent(request):
    doc = ConsentDocument.objects.order_by("-uploaded_at").first()
    if doc and doc.file:
        return redirect(doc.file.url)
    # アップロードが無い / 初期化で消えた → 静的ファイルへフォールバック
    return redirect(static("rtms_app/docs/consent_default.pdf"))