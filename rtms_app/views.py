from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time
from django.utils.safestring import mark_safe
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

from .models import Patient, TreatmentSession, MappingSession, Assessment, ConsentDocument, AuditLog, SideEffectCheck
from .forms import (
    PatientFirstVisitForm, MappingForm, TreatmentForm, 
    PatientRegistrationForm, AdmissionProcedureForm
)
from .utils.request_context import get_current_request, get_client_ip, get_user_agent, can_view_audit
from .services.side_effect_schema import SIDE_EFFECT_ITEMS
from .services.mapping_service import get_latest_mt_percent

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
    # admission_date に依存せず、治療開始日が設定されていれば
    # 対象日が治療開始日当日またはそれ以前なら baseline と見なす
    if not patient.first_treatment_date:
        return None

    if target_date <= patient.first_treatment_date:
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
        # baseline は治療開始日当日を含めて許可するため、期限は初回治療日までとする
        return patient.first_treatment_date
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
    
    # 位置決めの実績は週番号→実績日 へ集計し、カレンダー表示は「週の最初の平日」を計画とするが、
    # 実績がある場合は実績日に移動表示する
    mapping_week_to_date = {}
    for ms in MappingSession.objects.filter(patient=patient).order_by('-date'):
        if ms.week_number not in mapping_week_to_date:
            mapping_week_to_date[ms.week_number] = ms.date
    mapping_weeks_done = set(mapping_week_to_date.keys())
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
            
        # 2. 位置決め（週ごとの表示は後段でまとめて追加）
            
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
                        url_name = {
                            'baseline': 'assessment_baseline',
                            'week3': 'assessment_week3',
                            'week6': 'assessment_week6',
                        }.get(timing, 'assessment_baseline')
                        event = {
                            'type': 'assessment',
                            'label': label,
                            'url': build_url(url_name, [patient.id], query={'from': 'clinical_path'}),
                            'date': we,
                            'timing': timing,
                            'window_end': we
                        }
                        day['events'].append(event)
                        assessment_events.append(event)
                        break

    # 週ごとの「位置決め」イベントを追加（治療開始日を第1週1日目とし、その週の最初の平日）
    if treatment_start:
        ft = treatment_start
        week_idx = 1
        cur_week_start = ft
        while cur_week_start <= end_date:
            week_end = cur_week_start + timedelta(days=6)
            # 候補は治療開始と同じ曜日、その日が非治療日であれば週内で最初の治療日へシフト
            d = cur_week_start
            while d <= week_end and not is_treatment_day(d):
                d += timedelta(days=1)
            # 実績があれば表示日を実績日へ移動
            display_date = mapping_week_to_date.get(week_idx, d)
            if start_date <= display_date <= end_date:
                # カレンダー上の該当日にイベントを追加
                for week in calendar_weeks:
                    for day in week:
                        if day['date'] == display_date:
                            done = week_idx in mapping_weeks_done
                            label = f"第{week_idx}週目の位置決め" + (" (済)" if done else "")
                            # URLはフォーム初期化のため、date=display_date, week=week_idx を渡す
                            url = build_url("mapping_add", args=[patient.id], query={"date": display_date.strftime("%Y-%m-%d"), "week": week_idx})
                            day['events'].append({
                                'type': 'mapping',
                                'label': label,
                                'url': url,
                            })
                            break
            # 次の週
            cur_week_start += timedelta(days=7)
            week_idx += 1
            # 第7週目以降も、30回終了（推定日）までは表示を継続
            if treatment_end_est and cur_week_start > (treatment_end_est + timedelta(days=14)):
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
        if form.is_valid():
            m = form.save(commit=False)
            m.patient = patient
            # Auto-fill resting_mt and stimulation_site with defaults
            m.resting_mt = 50  # Default value
            m.stimulation_site = '左DLPFC'  # Default value
            m.save()
            return redirect(f"/app/dashboard/?date={dashboard_date}" if dashboard_date else 'rtms_app:dashboard')
    else:
        initial_date_raw = request.GET.get('date')
        initial_date = parse_date(initial_date_raw) if initial_date_raw else timezone.now().date()
        # デフォルト週番号：クエリ ?week= があれば採用、なければ開始日から算出
        week_param = request.GET.get('week')
        if week_param:
            try:
                week_default = int(week_param)
            except Exception:
                week_default = get_current_week_number(patient.first_treatment_date, initial_date) or 1
        else:
            week_default = get_current_week_number(patient.first_treatment_date, initial_date) or 1

        form = MappingForm(initial={
            'date': initial_date,
            'week_number': week_default,
            'stimulus_intensity_mt_percent': 120,
            'intensity_percent': 60
        })
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
                return redirect(build_url('rtms_app:print:patient_print_bundle', args=[patient.id], query=query))

            if dashboard_date:
                return redirect(f"{reverse('rtms_app:dashboard')}?date={dashboard_date}")
            return redirect('rtms_app:dashboard')
    else: form = PatientFirstVisitForm(instance=patient)
    floating_print_options = [{
        'label': '印刷プレビュー',
        'icon': 'fa-print',
        'href': reverse('rtms_app:print:patient_print_bundle', args=[patient.id]) + '?docs=admission&docs=suitability',
        'target': '_blank',
        'docs_form_id': 'bundlePrintFormFirstVisit',
    }]
    return render(request, 'rtms_app/patient_first_visit.html', {
        'patient': patient,
        'form': form,
        'referral_options': referral_options,
        'referral_map_json': json.dumps(referral_map_json, ensure_ascii=False),
        'end_date_est': end_date_est,
        'dashboard_date': dashboard_date,
        'hamd_items': hamd_items,
        'hamd_items_left': hamd_items_left,
        'hamd_items_right': hamd_items_right,
        'baseline_assessment': baseline_assessment,
        'floating_print_options': floating_print_options,
        'can_view_audit': can_view_audit(request.user),
    })

@login_required
def treatment_add(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date')
    latest_mapping = MappingSession.objects.filter(patient=patient).order_by('-date').first()
    target_date_str = request.GET.get('date')
    now = timezone.localtime(timezone.now())
    initial_date = parse_date(target_date_str) if target_date_str else now.date()

    def build_default_rows():
        rows = []
        # 新しい構造に対応：before, during, after, relatedness, memo
        default_items = [
            '頭皮痛・刺激痛',
            '顔面の不快感',
            '頸部痛・肩こり',
            '頭痛 (刺激後)',
            'けいれん (部位・時間)',
            '失神',
            '聴覚障害',
            'めまい・耳鳴り',
            '注意集中困難',
            '急性気分変化 (躁転など)',
            'その他'
        ]
        for item_name in default_items:
            row = {
                'item': item_name,
                'before': 0,
                'during': 0,
                'after': 0,
                'relatedness': 0,
                'memo': ''
            }
            rows.append(row)
        return rows

    session_num = get_session_count(patient, initial_date) + 1
    week_num = get_current_week_number(patient.first_treatment_date, initial_date)
    end_date_est = get_completion_date(patient.first_treatment_date)
    alert_msg = ""
    instruction_msg = ""
    mapping_alert = None
    
    # Get current week's mapping session
    current_week_mapping = None
    if week_num and week_num <= 6:
        current_week_mapping = MappingSession.objects.filter(
            patient=patient,
            week_number=week_num
        ).order_by('-date').first()
        
        if not current_week_mapping:
            mapping_alert = f"今週（第{week_num}週）の位置決めをしてください！"
    # 共通ロジックで第3週評価の推奨を計算
    from .services.recommendation import get_patient_recommendation
    rec = get_patient_recommendation(patient)
    is_remission = rec.status == 'remission'
    judgment_info = None if rec.status == 'pending' else rec.message
    if is_remission and week_num >= 4:
            weekly_count = get_weekly_session_count(patient, initial_date)
            current_weekly = weekly_count + 1
            if week_num == 4:
                if current_weekly > 3:
                    alert_msg = f"【制限超過】第4週(週3回まで)です。今回で週{current_weekly}回目になります。"
                else:
                    alert_msg = f"【漸減】第4週です。週3回まで (現在: 週{current_weekly}回目)"
            elif week_num == 5:
                if current_weekly > 2:
                    alert_msg = f"【制限超過】第5週(週2回まで)です。今回で週{current_weekly}回目になります。"
                else:
                    alert_msg = f"【漸減】第5週です。週2回まで (現在: 週{current_weekly}回目)"
            elif week_num == 6:
                if current_weekly > 1:
                    alert_msg = f"【制限超過】第6週(週1回まで)です。今回で週{current_weekly}回目になります。"
                else:
                    alert_msg = f"【漸減】第6週です。週1回まで (現在: 週{current_weekly}回目)"
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

            # 同期: 旧フィールドにも値を保持
            s.motor_threshold = s.mt_percent
            s.intensity = s.intensity_percent
            if s.intensity_percent is None and s.mt_percent is not None:
                s.intensity_percent = s.mt_percent
                s.intensity = s.mt_percent

            s.save()

            rows_raw = request.POST.get('side_effect_rows_json', '[]')
            try:
                rows = json.loads(rows_raw)
            except Exception:
                rows = build_default_rows()
            memo = request.POST.get('side_effect_memo', '')
            signature = request.POST.get('side_effect_signature', '')
            try:
                SideEffectCheck.objects.update_or_create(
                    session=s,
                    defaults={'rows': rows, 'memo': memo, 'physician_signature': signature}
                )
            except Exception as e:
                if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                    return JsonResponse({'status': 'error', 'message': f'SideEffect save failed: {e}'}, status=500)
                raise

            action = request.POST.get('action')

            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                redirect_url = build_url('dashboard', query={'date': dashboard_date}) if dashboard_date else build_url('dashboard')
                print_url = reverse('print:print_side_effect_check', args=[patient.id, s.id])
                return JsonResponse({'status': 'success', 'id': s.id, 'redirect_url': redirect_url, 'print_url': print_url})

            if action == 'print_side_effect':
                return redirect(reverse('print:print_side_effect_check', args=[patient.id, s.id]))

            return redirect(build_url('dashboard', query={'date': dashboard_date}) if dashboard_date else build_url('dashboard'))
        else:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'status': 'error', 'errors': form.errors}, status=400)
    else:
        latest_mt = get_latest_mt_percent(patient)
        initial_data = {
            'treatment_date': initial_date,
            'treatment_time': now.strftime('%H:%M'),
            'coil_type': 'BrainsWay H1',
            'target_site': '左DLPFC',
            'mt_percent': latest_mt if latest_mt is not None else 0,
            'intensity_percent': latest_mt if latest_mt is not None else 0,
            'frequency_hz': 18,
            'train_seconds': 2,
            'intertrain_seconds': 20,
            'train_count': 55,
            'total_pulses': 1980,
        }
        form = TreatmentForm(initial=initial_data)

    side_effect_rows = build_default_rows()
    # JSON生成：ensure_ascii=Falseで日本語をそのまま出力。テンプレート側でHTMLエスケープする。
    side_effect_rows_json = json.dumps(side_effect_rows, ensure_ascii=False, separators=(',', ':'))

    return render(
        request,
        'rtms_app/treatment_add.html',
        {
            'patient': patient,
            'form': form,
            'latest_mapping': latest_mapping,
            'current_week_mapping': current_week_mapping,
            'mapping_alert': mapping_alert,
            'side_effect_items': SIDE_EFFECT_ITEMS,
            'side_effect_rows_json': side_effect_rows_json,
            'session_num': session_num,
            'week_num': week_num,
            'end_date_est': end_date_est,
            'start_date': patient.first_treatment_date,
            'dashboard_date': dashboard_date,
            'alert_msg': alert_msg,
            'instruction_msg': instruction_msg,
            'judgment_info': judgment_info,
            'recommendation': rec.to_context(),
        },
    )

def _assessment_common(request, patient, timing, timing_label, dashboard_date, from_page):
    # HAM-D items definition (shared)
    hamd_items = [
        ('q1', '1. 抑うつ気分', 4, ""), ('q2', '2. 罪責感', 4, ""), ('q3', '3. 自殺', 4, ""),
        ('q4', '4. 入眠障害', 2, ""), ('q5', '5. 熟眠障害', 2, ""), ('q6', '6. 早朝睡眠障害', 2, ""),
        ('q7', '7. 仕事と活動', 4, ""), ('q8', '8. 精神運動抑制', 4, ""), ('q9', '9. 精神運動激越', 4, ""),
        ('q10', '10. 不安, 精神症状', 4, ""), ('q11', '11. 不安, 身体症状', 4, ""), ('q12', '12. 身体症状, 消化器系', 2, ""),
        ('q13', '13. 身体症状, 一般的', 2, ""), ('q14', '14. 生殖器症状', 2, ""), ('q15', '15. 心気症', 4, ""),
        ('q16', '16. 体重減少', 2, ""), ('q17', '17. 病識', 2, ""),
        ('q18', '18. 日内変動', 2, ""), ('q19', '19. 現実感喪失・離人症', 4, ""), ('q20', '20. 妄想症状', 3, ""),
        ('q21', '21. 強迫症状', 2, ""),
    ]
    hamd_items_left = hamd_items[:11]
    hamd_items_right = hamd_items[11:]

    existing_assessment = Assessment.objects.filter(patient=patient, timing=timing, type='HAM-D').order_by('-date').first()

    if request.method == 'POST':
        try:
            date_str = request.POST.get('date') or timezone.now().date().isoformat()
            date = datetime.date.fromisoformat(date_str)

            scores = {}
            for key, _, maxv, _ in hamd_items:
                v = request.POST.get(key, "0")
                try:
                    iv = int(v)
                except Exception:
                    iv = 0
                iv = max(0, min(iv, maxv))
                scores[key] = iv

            note = (request.POST.get('note') or "").strip()

            assessment, _ = Assessment.objects.get_or_create(patient=patient, timing=timing, defaults={'date': date, 'scores': scores, 'note': note})
            assessment.date = date
            assessment.scores = scores
            assessment.note = note
            assessment.calculate_scores()
            assessment.save()

            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({
                    'status': 'success',
                    'id': assessment.id,
                    'total_17': assessment.total_score_17,
                })

            if from_page == 'clinical_path':
                q = {'focus': assessment.date.strftime('%Y-%m-%d')}
                if dashboard_date:
                    q['dashboard_date'] = dashboard_date
                return redirect(build_url('patient_clinical_path', args=[patient.id], query=q))

            if dashboard_date:
                return redirect(build_url('dashboard', query={'date': dashboard_date}))
            return redirect(build_url('dashboard'))

        except Exception:
            import traceback
            traceback.print_exc()
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'status': 'error', 'message': '保存に失敗しました。'}, status=400)
            return HttpResponse("保存に失敗しました。", status=400)

    ctx = {
        'patient': patient,
        'today': timezone.now().date().isoformat(),
        'dashboard_date': dashboard_date,
        'timing': timing,
        'timing_label': timing_label,
        'existing_assessment': existing_assessment,
        'hamd_items_left': hamd_items_left,
        'hamd_items_right': hamd_items_right,
    }
    template_name = f"rtms_app/assessment/{timing}.html"
    return render(request, template_name, ctx)


@login_required
def assessment_baseline(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date')
    from_page = request.GET.get('from')
    return _assessment_common(request, patient, 'baseline', '治療前評価', dashboard_date, from_page)


@login_required
def assessment_week3(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date')
    from_page = request.GET.get('from')
    return _assessment_common(request, patient, 'week3', '3週目評価', dashboard_date, from_page)


@login_required
def assessment_week6(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date')
    from_page = request.GET.get('from')
    return _assessment_common(request, patient, 'week6', '6週目評価', dashboard_date, from_page)


# backward compatibility for old timing URL
@login_required
def assessment_add(request, patient_id, timing):
    # redirect to the new fixed-timing views to avoid mixed state
    if timing == 'baseline':
        return assessment_baseline(request, patient_id)
    if timing == 'week3':
        return assessment_week3(request, patient_id)
    if timing == 'week6':
        return assessment_week6(request, patient_id)
    return HttpResponse(status=400)

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
                    'redirect_url': reverse("rtms_app:print:patient_print_discharge", args=[patient.id]),
                })
            if action == 'print_referral':
                return JsonResponse({
                    'status': 'success',
                    'redirect_url': reverse("rtms_app:print:patient_print_referral", args=[patient.id]),
                })
            else:
                redirect_url = f"{reverse('rtms_app:dashboard')}?date={dashboard_date}" if dashboard_date else reverse('rtms_app:dashboard')
                return JsonResponse({'status': 'success', 'redirect_url': redirect_url})

        # ★ 通常POST（非AJAX）
        if action == 'print_bundle':
            return redirect(
                build_url(
                    'rtms_app:print:patient_print_bundle',
                    args=[patient.id],
                    query={'docs': ['discharge', 'referral']},
                )
            )
        if action == 'print_discharge':
            return redirect(reverse("rtms_app:print:patient_print_discharge", args=[patient.id]))
        if action == 'print_referral':
            return redirect(reverse("rtms_app:print:patient_print_referral", args=[patient.id]))

        return redirect(f"/app/dashboard/?date={dashboard_date}" if dashboard_date else 'rtms_app:dashboard')

        
    sessions_qs = TreatmentSession.objects.filter(patient=patient).order_by('date')
    # 同一日付の重複を最新（日時が最大）1件に集約
    latest_session_by_date = {}
    for s in sessions_qs:
        k = s.date.date()
        if k not in latest_session_by_date or latest_session_by_date[k].date < s.date:
            latest_session_by_date[k] = s
    sessions = [latest_session_by_date[d] for d in sorted(latest_session_by_date.keys())]

    assessments_qs = Assessment.objects.filter(patient=patient).order_by('date')
    latest_assessment_by_date = {}
    for a in assessments_qs:
        k = a.date
        latest_assessment_by_date[k] = a  # 同日複数あれば最後のものを残す
    test_scores = [latest_assessment_by_date[d] for d in sorted(latest_assessment_by_date.keys())]
    score_admin = next((a for a in test_scores if a.timing == 'baseline'), None)
    score_w3 = next((a for a in test_scores if a.timing == 'week3'), None)
    score_w6 = next((a for a in test_scores if a.timing == 'week6'), None)
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
    if patient.summary_text:
        summary_text = patient.summary_text
    else:
        from .services.recommendation import get_patient_recommendation
        rec = get_patient_recommendation(patient)
        rec_str = ""
        if score_w3 and (score_w3.total_score_17 or score_w3.total_score_21) and rec.status != 'pending':
            # 改善率（%）はRecommendationから、評価ラベルはmessageから抽出
            rate = rec.improvement_rate
            rate_pct = f"{int(rate*100)}%" if rate is not None else "-"
            eval_label = '寛解' if rec.status == 'remission' else ('有効性あり' if rec.status == 'effective' else '治療無効')
            rec_str = f"（改善率{rate_pct}、評価：{eval_label}）"
        summary_text = (
            f"{created_at_str}初診、{admission_date_str}任意入院。\n"
            f"入院時{fmt_score(score_admin)}、{start_date_str}から全{total_count}回のrTMS治療を実施した。\n"
            f"3週時、{fmt_score(score_w3)}{rec_str}、6週時、{fmt_score(score_w6)}となった。\n"
            f"治療中の合併症：{side_effects_summary}。\n"
            f"{end_date_str}退院。紹介元へ逆紹介、抗うつ薬の治療継続を依頼した。"
        )
    bundle_url = reverse("rtms_app:print:patient_print_bundle", args=[patient.id])
    qs = urlencode([("docs", "discharge"), ("docs", "referral")])
    floating_print_options = [
        {
            "label": "印刷プレビュー",
            "icon": "fa-print",
            "href": f"{bundle_url}?{qs}",
            "target": "_blank",
            "docs_form_id": "bundlePrintFormDischarge",
        },
    ]
    from .services.recommendation import get_patient_recommendation
    rec = get_patient_recommendation(patient)
    return render(request, 'rtms_app/patient_summary.html', {
        'patient': patient,
        'summary_text': summary_text,
        'history_list': history_list,
        'today': timezone.now().date(),
        'test_scores': test_scores,
        'dashboard_date': dashboard_date,
        'floating_print_options': floating_print_options,
        'can_view_audit': can_view_audit(request.user),
        'recommendation': rec.to_context(),
    })
    
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
    return redirect(build_url("rtms_app:print:patient_print_bundle", args=[patient.id], query=query))

def _render_patient_summary(request, patient, mode):
    normalized_mode = 'discharge' if mode == 'summary' else mode
    query = {"docs": [normalized_mode]}
    return_to = request.GET.get("return_to") or request.META.get("HTTP_REFERER")
    if return_to:
        query["return_to"] = return_to
    return redirect(build_url("rtms_app:print:patient_print_bundle", args=[patient.id], query=query))


def patient_print_summary(request, pk):
    patient = get_object_or_404(Patient, pk=pk)
    mode = request.GET.get('mode', 'discharge')
    return _render_patient_summary(request, patient, mode)

@login_required
@login_required
def consent_latest(request):
    return render(request, "rtms_app/consent_latest.html")

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
        'href': reverse('rtms_app:print:print_clinical_path', args=[patient.id]),
        'target': '_blank'
    }]
    from .services.recommendation import get_patient_recommendation
    rec = get_patient_recommendation(patient)
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
        'can_view_audit': can_view_audit(request.user),
        'recommendation': rec.to_context(),
    })

@login_required
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