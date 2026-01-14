from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time
from django.urls import reverse
from django.templatetags.static import static
from django.utils.safestring import mark_safe
from datetime import timedelta, date
import datetime
from django.http import HttpResponse, FileResponse, JsonResponse
from django.conf import settings
from django.contrib.auth import logout
from django.db.models import Q, Count
from django.core.exceptions import PermissionDenied
from functools import wraps
from calendar import monthrange
from collections import defaultdict
import os
import csv
import json
import io
from urllib.parse import urlencode
import logging

try:
    import jpholiday
except ImportError:
    jpholiday = None

from .models import (
    Patient,
    TreatmentSession,
    MappingSession,
    Assessment,
    ConsentDocument,
    AuditLog,
    SideEffectCheck,
    ScaleDefinition,
    TimingScaleConfig,
    AssessmentRecord,
)
from .forms import (
    PatientFirstVisitForm, MappingForm, TreatmentForm,
    PatientRegistrationForm, PatientBasicEditForm, AdmissionProcedureForm
)
from .utils.request_context import get_current_request, get_client_ip, get_user_agent, can_view_audit
from .services.rtms_schedule import (
    generate_treatment_dates,
    generate_mapping_dates,
    session_info_for_date,
    format_rtms_label,
)
from .services.schedule import shift_future_sessions
from .utils.hamd import classify_hamd_response, classify_hamd17_severity


def superuser_required(view_func):
    """
    Decorator to require superuser authentication for a view.
    Redirects unauthenticated users to login and raises PermissionDenied for non-superusers.
    """
    @wraps(view_func)
    def _wrapped_view(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect(f"{reverse('admin:login')}?next={request.path}")
        if not request.user.is_superuser:
            raise PermissionDenied("スーパーユーザーのみこの機能にアクセスできます。")
        return view_func(request, *args, **kwargs)
    return _wrapped_view


def _questionnaire_questions():
    questions_past = [
        {'no': 1, 'key': 'q_past_rtms', 'label': 'rTMS実施経験（治験・研究を含む）'},
        {'no': 2, 'key': 'q_past_side_effect', 'label': 'rTMS後に副作用などの不快な経験'},
        {'no': 3, 'key': 'q_past_ect', 'label': '電気けいれん療法（ECT）の実施歴'},
        {'no': 4, 'key': 'q_past_seizure', 'label': 'けいれん発作（てんかん診断の有無を問わない）'},
        {'no': 5, 'key': 'q_past_loc', 'label': '意識消失発作'},
        {'no': 6, 'key': 'q_past_stroke', 'label': '脳卒中（脳梗塞・脳出血など）'},
        {'no': 7, 'key': 'q_past_trauma', 'label': '頭部外傷（意識消失を伴うなど重度なもの）'},
        {'no': 8, 'key': 'q_past_surgery', 'label': '頭部の手術歴'},
        {'no': 9, 'key': 'q_past_neuro', 'label': '脳外科もしくは神経内科の病気'},
        {'no': 10, 'key': 'q_past_internal', 'label': '脳障害をおこす可能性のある内科疾患'},
        {'no': 11, 'key': 'q_past_abuse', 'label': 'アルコールや薬物の乱用'},
    ]
    questions_current = [
        {'no': 12, 'key': 'q_cur_headache', 'label': '頻繁または重度な頭痛'},
        {'no': 13, 'key': 'q_cur_metal', 'label': '頭の中に金属や磁性体（チタン製品かどうか要確認）'},
        {'no': 14, 'key': 'q_cur_device', 'label': '体内埋め込み式の医療機器（心臓ペースメーカーなど）'},
        {'no': 15, 'key': 'q_cur_abuse', 'label': '多量の飲酒や薬物の乱用'},
        {'no': 16, 'key': 'q_cur_preg', 'label': '妊娠中、もしくは妊娠の可能性が否定されない'},
        {'no': 17, 'key': 'q_cur_family_epilepsy', 'label': '家族内にてんかんを持っているかた'},
    ]
    keys = [q['key'] for q in (questions_past + questions_current)] + ['q_details']
    return questions_past, questions_current, keys

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
    elif timing == "week4":
        raw_start = ft + timedelta(days=21)
        raw_end = ft + timedelta(days=27)
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


def compute_initials_from_name(name: str) -> str:
    """Generate initials from a patient's name (surname + given name first characters)."""
    if not name:
        return ''
    parts = [p for p in name.replace('　', ' ').split(' ') if p]
    if not parts:
        return ''
    initials = []
    for p in parts[:2]:
        c = p[0]
        initials.append(c.upper() if c.isalpha() else c)
    return '.'.join(initials)


def build_substance_use_summary(session: TreatmentSession | None) -> str:
    """Summarize alcohol/caffeine and medication change flags from a session."""
    if not session:
        return ''
    labels = []
    if session.safety_alcohol is False:
        labels.append("飲酒/過量カフェイン: 摂取あり")
    else:
        labels.append("飲酒/過量カフェイン: 摂取なし")
    if session.safety_meds is False:
        labels.append("薬剤変更あり")
    else:
        labels.append("薬剤変更なし")
    return " / ".join(labels)


def resolve_contact_person(patient: Patient | None, session: TreatmentSession | None, user) -> str:
    """Resolve the contact/attending person name with fallbacks."""
    candidates = []
    if session and session.performer:
        candidates.append(session.performer)
    if patient and patient.attending_physician:
        candidates.append(patient.attending_physician)
    if user and getattr(user, 'is_authenticated', False):
        candidates.append(user)

    for cand in candidates:
        name = cand.get_full_name() if hasattr(cand, 'get_full_name') else ''
        if name:
            return name
        if hasattr(cand, 'username') and cand.username:
            return cand.username
    return ''


def get_latest_resting_mt(patient: Patient | None, course_number: int | None, session_date: date | None, week_num: int | None = None):
    """Fetch the most recent resting MT for the same course/week up to the session date."""
    if not patient:
        return None
    qs = MappingSession.objects.filter(patient=patient)
    if course_number:
        qs = qs.filter(course_number=course_number)
    if session_date:
        qs = qs.filter(date__lte=session_date)
    if week_num:
        qs = qs.filter(week_number=week_num)
    mapping = qs.order_by('-date').first()
    return mapping.resting_mt if mapping else None


def get_daily_treatment_number(patient: Patient | None, course_number: int | None, session_date: date | None):
    """Return the count of sessions for the patient on the given date (per course)."""
    if not patient or not session_date:
        return None
    qs = TreatmentSession.objects.filter(patient=patient, session_date=session_date)
    if course_number:
        qs = qs.filter(course_number=course_number)
    return qs.count() or None


def get_cumulative_treatment_number(patient: Patient | None, course_number: int | None, session_id: int | None):
    """Get cumulative (ordinal) treatment session number within the course."""
    if not patient or not session_id:
        return None
    try:
        session = TreatmentSession.objects.get(pk=session_id)
    except TreatmentSession.DoesNotExist:
        return None
    
    qs = TreatmentSession.objects.filter(patient=patient, course_number=course_number or session.course_number)
    qs = qs.order_by('session_date', 'date', 'id')
    
    cumulative_no = 1
    for ts in qs:
        if ts.id == session.id:
            return cumulative_no
        cumulative_no += 1
    return None


def convert_to_romaji_initials(name_ja: str) -> str:
    """
    Estimate romaji initials from Japanese name.
    Priority: existing romaji field if available, else simplified hiragana→romaji, else take first 2 chars as X.X.
    """
    if not name_ja:
        return ''
    
    # Simple hiragana→romaji mapping (subset, for common names)
    hiragana_map = {
        'あ': 'a', 'い': 'i', 'う': 'u', 'え': 'e', 'お': 'o',
        'か': 'ka', 'き': 'ki', 'く': 'ku', 'け': 'ke', 'こ': 'ko',
        'が': 'ga', 'ぎ': 'gi', 'ぐ': 'gu', 'げ': 'ge', 'ご': 'go',
        'さ': 'sa', 'し': 'si', 'す': 'su', 'せ': 'se', 'そ': 'so',
        'ざ': 'za', 'じ': 'zi', 'ず': 'zu', 'ぜ': 'ze', 'ぞ': 'zo',
        'た': 'ta', 'ち': 'ti', 'つ': 'tu', 'て': 'te', 'と': 'to',
        'だ': 'da', 'ぢ': 'di', 'づ': 'du', 'で': 'de', 'ど': 'do',
        'な': 'na', 'に': 'ni', 'ぬ': 'nu', 'ね': 'ne', 'の': 'no',
        'は': 'ha', 'ひ': 'hi', 'ふ': 'hu', 'へ': 'he', 'ほ': 'ho',
        'ば': 'ba', 'び': 'bi', 'ぶ': 'bu', 'べ': 'be', 'ぼ': 'bo',
        'ぱ': 'pa', 'ぴ': 'pi', 'ぷ': 'pu', 'ぺ': 'pe', 'ぽ': 'po',
        'ま': 'ma', 'み': 'mi', 'む': 'mu', 'め': 'me', 'も': 'mo',
        'や': 'ya', 'ゆ': 'yu', 'よ': 'yo',
        'ら': 'ra', 'り': 'ri', 'る': 'ru', 'れ': 're', 'ろ': 'ro',
        'わ': 'wa', 'を': 'wo', 'ん': 'n',
    }
    
    # Try to convert to romaji and extract initials
    parts = name_ja.replace('　', ' ').split(' ')
    initials = []
    for part in parts[:2]:
        if not part:
            continue
        # For each part, take first char and try to romanize
        first_char = part[0]
        if first_char in hiragana_map:
            rom = hiragana_map[first_char]
            initials.append(rom[0].upper())
        elif ord(first_char) >= 0x4E00 and ord(first_char) <= 0x9FFF:  # Kanji
            # Kanji: fallback to just first character
            initials.append(first_char)
        else:
            # Alphabet or other: use as-is
            initials.append(first_char.upper() if first_char.isalpha() else first_char)
    
    if not initials:
        # Fallback: take first 2 chars from name as X.X.
        try:
            return f"{name_ja[0]}.{name_ja[1]}" if len(name_ja) >= 2 else f"{name_ja[0]}"
        except:
            return ''
    
    if len(initials) == 1:
        return initials[0]
    return '.'.join(initials[:2])

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
    # Canonical 30回目は開院日に基づく予定
    treatment_end_est = None
    if treatment_start:
        tdates_for_end = generate_treatment_dates(treatment_start, total=30, holidays=JP_HOLIDAYS)
        if tdates_for_end:
            treatment_end_est = tdates_for_end[-1]
    
    base_end = patient.discharge_date
    if not base_end:
        if treatment_end_est:
            base_end = treatment_end_est  # 30回目当日まで
        else:
            base_end = base_start + timedelta(days=30)
            
    # 開始日が月曜になるように調整
    start_date = base_start - timedelta(days=base_start.weekday())
    
    # 終了日はその日まで（週末への拡張はしない）
    end_date = base_end

    calendar_weeks = []
    current_week = []
    current = start_date
    
    mapping_dates = list(MappingSession.objects.filter(patient=patient).values_list('date', flat=True))
    treatments_done = {t.date.date(): t for t in TreatmentSession.objects.filter(patient=patient)}
    assessment_events = []  # 評価イベントを別途収集

    # Canonical planned treatment and mapping dates (no drift, closures honored)
    treat_dates = []
    scheduled_mapping_dates = set()
    if treatment_start:
        treat_dates = generate_treatment_dates(treatment_start, total=30, holidays=JP_HOLIDAYS)
        # Use mapping base as patient.mapping_date if set, else first_treatment_date
        mapping_base = patient.mapping_date or treatment_start
        if mapping_base:
            mapping_list = generate_mapping_dates(mapping_base, weeks=8, holidays=JP_HOLIDAYS)
            scheduled_mapping_dates = {m['actual'] for m in mapping_list}
        # Set estimated end to the 30th treatment date
        if treat_dates:
            treatment_end_est = treat_dates[-1]

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
            
        # 2. 位置決め（実績があれば実績、なければ毎週の予定を表示）
        if current == patient.mapping_date or current in mapping_dates:
            day_info['events'].append({
                'type': 'mapping',
                'label': '位置決め',
                'url': build_url("mapping_add", args=[patient.id], query={"date": current.strftime("%Y-%m-%d")})
            })
        elif current in scheduled_mapping_dates:
            day_info['events'].append({
                'type': 'mapping',
                'label': '位置決め',
                'url': build_url("mapping_add", args=[patient.id], query={"date": current.strftime("%Y-%m-%d")})
            })
            
        # 3. 治療予定・実績（canonical treat_dates を基準に表示）
        if treatment_start and current in treat_dates:
            idx = treat_dates.index(current)
            session_no = idx + 1
            # Week number rolls over on the same weekday anchored to first treatment date
            week_no = get_current_week_number(treatment_start, current)
            status_label = " (済)" if current in treatments_done else ""
            label = format_rtms_label(session_no, week_no)
            day_info['events'].append({
                'type': 'treatment',
                'label': label + status_label,
                'url': build_url('treatment_add', [patient.id], {'date': current})
            })
        
        # 5. 退院
        if current == patient.discharge_date:
            day_info['events'].append({'type': 'discharge', 'label': '退院準備', 'url': build_url('patient_home', [patient.id])})

        elif not patient.discharge_date and treatment_start:
            # Show discharge prep on the 30th treatment date (not next day)
            if treatment_end_est and current == treatment_end_est:
                day_info['events'].append({'type': 'discharge', 'label': '退院準備', 'url': build_url('patient_home', [patient.id])})

        current_week.append(day_info)
        
        if current.weekday() == 6:
            calendar_weeks.append(current_week)
            current_week = []
            
        current += timedelta(days=1)
        
    if current_week: calendar_weeks.append(current_week)
    
    # 評価イベントを window_end に追加
    for timing in ['baseline', 'week3', 'week4', 'week6']:
        ws, we = get_assessment_window(patient, timing)
        if we and start_date <= we <= end_date:
            # 該当日の day_info を探す
            for week in calendar_weeks:
                for day in week:
                    if day['date'] == we:
                        existing = Assessment.objects.filter(patient=patient, timing=timing).exists()
                        # 括弧書き（HAM-D）と日付 (mm/dd) を除去したシンプル表記
                        label = {
                            'baseline': '治療前評価',
                            'week3': '第3週目評価',
                            'week4': '第4週目評価',
                            'week6': '第6週目評価'
                        }.get(timing, timing)
                        if existing:
                            label += ' (済)'
                        
                        # Use specific URL for week4, generic for others
                        if timing == 'week4':
                            url = build_url('assessment_week4', [patient.id], query={'from': 'clinical_path', 'date': we.strftime('%Y-%m-%d')})
                        else:
                            url = build_url('assessment_add', [patient.id, timing], query={'from': 'clinical_path', 'date': we.strftime('%Y-%m-%d')})
                            
                        event = {
                            'type': 'assessment',
                            'label': label,
                            'url': url,
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
    # Q18: only B (degree of diurnal variation) is relevant for scoring/UI — A (timing) removed from display
    "q18": "B. 日内変動がある場合、変動の程度をマークする。\n0. なし\n1. 軽度\n2. 重度",
    "q19": "0. なし\n1. 軽度\n2. 中等度\n3. 重度\n4. 何もできなくなる",
    "q20": "0. なし\n1. 疑念をもっている\n2. 関係念慮\n3. 被害関係妄想",
    "q21": "0. なし\n1. 軽度\n2. 重度",
}


def _hamd_items():
    items = [
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
        ('q16', '16. 体重減少', 2, HAMD_ANCHORS['q16']),
        ('q17', '17. 病識', 2, HAMD_ANCHORS['q17']),
        ('q18', '18. 日内変動', 2, HAMD_ANCHORS['q18']),
        ('q19', '19. 現実感喪失・離人症', 4, HAMD_ANCHORS['q19']),
        ('q20', '20. 妄想症状', 3, HAMD_ANCHORS['q20']),
        ('q21', '21. 強迫症状', 2, HAMD_ANCHORS['q21']),
    ]
    return items, items[:11], items[11:]

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
        # Use canonical treat_dates for session/week labels
        info = None
        if p.first_treatment_date:
            tdates = generate_treatment_dates(p.first_treatment_date, total=30, holidays=JP_HOLIDAYS)
            if target_date in tdates:
                idx = tdates.index(target_date)
                info = {
                    'session_no': idx + 1,
                    # Week number rolls over on the same weekday anchored to first treatment date
                    'week_no': get_current_week_number(p.first_treatment_date, target_date)
                }

        if info:
            n = info['session_no']
            week = info['week_no']
            today_session = TreatmentSession.objects.filter(patient=p, date__date=target_date).first(); is_done = today_session is not None
            todo_label = format_rtms_label(n, week)
            task_treatment.append({'obj': p, 'note': '', 'status': "実施済" if is_done else "実施未", 'color': "success" if is_done else "danger", 'session_num': n, 'todo': todo_label})
        
        # Use get_assessment_window() for week3/week4/week6 to match clinical path windows
        for timing_code, label_name in [('week3', '第3週目評価'), ('week4', '第4週目評価'), ('week6', '第6週目評価')]:
            ws, we = get_assessment_window(p, timing_code)
            if ws and we and target_date == we:
                assessment = Assessment.objects.filter(patient=p, timing=timing_code, date__range=[ws, we]).first()
                if assessment:
                    # mark as done
                    task_assessment.append({'obj': p, 'status': "実施済", 'color': "success", 'timing_code': timing_code, 'todo': f"{label_name} (完了)"})
                else:
                    task_assessment.append({'obj': p, 'status': "実施未", 'color': "danger", 'timing_code': timing_code, 'todo': f"{label_name} ({we.strftime('%m/%d')})"})
        # Discharge readiness is handled below via confirmed/estimated dates; avoid DB-count based labels

    # 退院準備: 退院日が確定している患者
    discharge_patients = Patient.objects.filter(discharge_date=target_date)
    for p in discharge_patients:
        task_discharge.append({'obj': p, 'status': "退院準備", 'color': "info", 'todo': "サマリー・紹介状作成"})

    # 退院準備: 退院日未設定だが30回目治療日の患者（同日に表示）
    for p in active_candidates:
        if p.discharge_date: continue  # 既に上記で追加済み
        if p.first_treatment_date:
            tdates = generate_treatment_dates(p.first_treatment_date, total=30, holidays=JP_HOLIDAYS)
            treatment_end_est = tdates[-1] if tdates else None
        else:
            treatment_end_est = None
        if treatment_end_est and target_date == treatment_end_est:
            task_discharge.append({'obj': p, 'status': "退院準備（予定）", 'color': "info", 'todo': "サマリー・紹介状作成"})

    dashboard_tasks = [{'list': task_first_visit, 'title': "① 初診", 'color_class': "bg-g-first-visit", 'icon': "fa-user-plus"}, {'list': task_admission, 'title': "② 入院", 'color_class': "bg-g-admission", 'icon': "fa-procedures"}, {'list': task_mapping, 'title': "③ 位置決め", 'color_class': "bg-g-mapping", 'icon': "fa-crosshairs"}, {'list': task_treatment, 'title': "④ 治療実施", 'color_class': "bg-g-treatment", 'icon': "fa-bolt"}, {'list': task_assessment, 'title': "⑤ 尺度評価", 'color_class': "bg-g-assessment", 'icon': "fa-clipboard-check"}, {'list': task_discharge, 'title': "⑥ 退院準備", 'color_class': "bg-g-discharge", 'icon': "fa-file-export"}]
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
        if form.is_valid():
            proc = form.save(commit=False)
            proc.is_admission_procedure_done = True
            # Ensure patient status reflects admission: set to inpatient when admission procedure completed
            if getattr(proc, 'status', None) != 'inpatient':
                proc.status = 'inpatient'
            proc.save(update_fields=['is_admission_procedure_done', 'status'])
            return redirect(f"/app/dashboard/?date={dashboard_date}" if dashboard_date else 'rtms_app:dashboard')
    else: form = AdmissionProcedureForm(instance=patient)
    return render(request, 'rtms_app/admission_procedure.html', {'patient': patient, 'form': form, 'dashboard_date': dashboard_date})

@login_required
def mapping_add(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date')
    history = MappingSession.objects.filter(patient=patient).order_by('date')
    
    # Determine initial date: GET date > existing > today
    date_param = request.GET.get('date') or request.GET.get('dashboard_date')
    if date_param:
        try:
            initial_date = datetime.datetime.strptime(date_param, '%Y-%m-%d').date()
        except:
            initial_date = timezone.localdate()
    else:
        initial_date = timezone.localdate()
    
    # Calculate week_no with day-based rollover anchored to first treatment date
    week_no_default = 1
    if patient.first_treatment_date:
        week_no_default = get_current_week_number(patient.first_treatment_date, initial_date)
    
    if request.method == 'POST':
        form = MappingForm(request.POST)
        if form.is_valid():
            inst = form.save(commit=False)
            course_number = patient.course_number or 1
            inst.patient = patient
            inst.course_number = course_number
            key_date = inst.date
            key_site = getattr(inst, 'stimulation_site', None) or MappingSession._meta.get_field('stimulation_site').get_default()
            defaults = {
                'week_number': inst.week_number,
                'resting_mt': inst.resting_mt,
                'stimulation_site': inst.stimulation_site,
                'helmet_position_a_x': inst.helmet_position_a_x,
                'helmet_position_a_y': inst.helmet_position_a_y,
                'helmet_position_b_x': inst.helmet_position_b_x,
                'helmet_position_b_y': inst.helmet_position_b_y,
                'notes': inst.notes,
                'course_number': course_number,
            }
            MappingSession.objects.update_or_create(
                patient=patient, 
                course_number=course_number, 
                date=key_date, 
                stimulation_site=key_site, 
                defaults=defaults
            )
            # Handle action: go to treatment or return to dashboard
            action = request.POST.get('action', '')
            if action == 'to_treatment':
                # Redirect to treatment_add with date parameter
                query_params = {'date': key_date.strftime('%Y-%m-%d')}
                if dashboard_date:
                    query_params['dashboard_date'] = dashboard_date
                return redirect(build_url('treatment_add', args=[patient.id], query=query_params))
            else:
                # Return to dashboard (default behavior for 'save_and_return')
                if dashboard_date:
                    return redirect(f"/app/dashboard/?date={dashboard_date}")
                return redirect('rtms_app:dashboard')
    else:
        form = MappingForm(initial={
            'date': initial_date,
            'week_number': week_no_default,
            'resting_mt': 60,
            'helmet_position_a_x': 3,
            'helmet_position_a_y': 1,
            'helmet_position_b_x': 9,
            'helmet_position_b_y': 1,
        })
    
    return render(request, 'rtms_app/mapping_add.html', {
        'patient': patient,
        'form': form,
        'history': history,
        'dashboard_date': dashboard_date,
        'week_no_default': week_no_default,
        'can_view_audit': can_view_audit(request.user),
        # Unified plan bar variables
        'treatment_plan_start': patient.first_treatment_date,
        'treatment_plan_end': get_completion_date(patient.first_treatment_date),
        'today_session_no': get_session_number(patient.first_treatment_date, initial_date),
        'total_sessions': 30,
        'week_no': week_no_default,
    })

@login_required
def patient_first_visit(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date')

    # Referral source/doctor are entered at patient registration; first-visit UI doesn't edit them.
    end_date_est = get_completion_date(patient.first_treatment_date)

    # ---- HAM-D modal (baseline) ----
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
        ('q16', '16. 体重減少', 2, HAMD_ANCHORS['q16']),
        ('q17', '17. 病識', 2, HAMD_ANCHORS['q17']),
        ('q18', '18. 日内変動', 2, HAMD_ANCHORS['q18']),
        ('q19', '19. 現実感喪失・離人症', 4, HAMD_ANCHORS['q19']),
        ('q20', '20. 妄想症状', 3, HAMD_ANCHORS['q20']),
        ('q21', '21. 強迫症状', 2, HAMD_ANCHORS['q21']),
    ]
    hamd_items_left = hamd_items[:11]
    hamd_items_right = hamd_items[11:]

    baseline_assessment = Assessment.objects.filter(patient=patient, timing='baseline').first()

    # ---- Questionnaire (used by modal) ----
    questions_past, questions_current, questionnaire_keys = _questionnaire_questions()
    questionnaire = patient.questionnaire_data or {}
    questionnaire_done = bool(questionnaire)

    if request.method == 'POST':
        # ---- HAM-D ajax save (baseline) ----
        if 'hamd_ajax' in request.POST:
            try:
                scores = {}
                for key, _, maxv, _ in hamd_items:
                    v = request.POST.get(key, "0")
                    try:
                        iv = int(v)
                    except Exception:
                        iv = 0
                    iv = max(0, min(iv, maxv))
                    scores[key] = iv

                note = (request.POST.get('hamd_note') or '').strip()

                course_number = patient.course_number or 1
                defaults = {
                    'date': timezone.now().date(),
                    'scores': scores,
                    'note': note,
                    'type': 'HAM-D',
                    'course_number': course_number,
                }
                assessment, _created = Assessment.objects.update_or_create(
                    patient=patient,
                    course_number=course_number,
                    timing='baseline',
                    type='HAM-D',
                    defaults=defaults,
                )

                total = assessment.total_score_17
                if 14 <= total <= 18:
                    severity = "中等症"
                    msg = "中等症と判定しました。rTMS適正質問票を確認してください。"
                elif total >= 19:
                    severity = "重症"
                    msg = "重症と判定しました。"
                elif 8 <= total <= 13:
                    severity = "軽症"
                    msg = ""
                else:
                    severity = "正常"
                    msg = ""

                return JsonResponse({
                    'status': 'success',
                    'total_17': total,
                    'severity': severity,
                    'message': msg,
                })
            except Exception as e:
                return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

        # card_id/name/birth_date/gender are registered at patient_add; keep stable here.
        # If the template omits these fields (read-only), ensure POST still validates.
        post = request.POST.copy()
        for f in ('card_id', 'name', 'birth_date', 'gender'):
            if not (post.get(f) or '').strip():
                v = getattr(patient, f)
                if hasattr(v, 'isoformat'):
                    v = v.isoformat()
                post[f] = str(v)

        form = PatientFirstVisitForm(post, instance=patient)
        if form.is_valid():
            p = form.save(commit=False); diag_list = request.POST.getlist('diag_list'); diag_other = request.POST.get('diag_other', '').strip()
            full_diagnosis = ", ".join(diag_list);
            if diag_other: full_diagnosis += f", その他({diag_other})"
            p.diagnosis = full_diagnosis

            # Questionnaire is submitted together with the first-visit form
            q_data = {}
            for k in questionnaire_keys:
                if k == 'q_details':
                    continue
                v = (request.POST.get(k) or '').strip()
                q_data[k] = v if v in ('はい', 'いいえ') else 'いいえ'
            q_data['q_details'] = (request.POST.get('q_details') or '').strip()
            p.questionnaire_data = q_data

            # Save protocol_type (form or POST override)
            protocol_type = request.POST.get('protocol_type') or 'INSURANCE'
            p.protocol_type = protocol_type

            p.save()

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
    else:
        form = PatientFirstVisitForm(instance=patient)
    floating_print_options = [{
        'label': '印刷プレビュー',
        'value': 'print_bundle',
        'icon': 'fa-print',
        'formaction': reverse('rtms_app:print:patient_print_bundle', args=[patient.id]),
        'formtarget': '_blank',
        'docs_form_id': 'bundlePrintFormFirstVisit',
    }]
    return render(request, 'rtms_app/patient_first_visit.html', {
        'patient': patient,
        'form': form,
        'end_date_est': end_date_est,
        'dashboard_date': dashboard_date,
        'baseline_assessment': baseline_assessment,
        'questionnaire_done': questionnaire_done,
        'questionnaire': questionnaire,
        'questions_past': questions_past,
        'questions_current': questions_current,
        'hamd_items_left': hamd_items_left,
        'hamd_items_right': hamd_items_right,
        'floating_print_options': floating_print_options,
        'can_view_audit': can_view_audit(request.user),
        'can_edit_basic': can_view_audit(request.user),
    })


@login_required
def patient_basic_edit(request, patient_id):
    if not can_view_audit(request.user):
        return HttpResponse("アクセス権限がありません。", status=403)

    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date')

    if request.method == 'POST':
        form = PatientBasicEditForm(request.POST, instance=patient)
        if form.is_valid():
            form.save()
            if dashboard_date:
                return redirect(f"{reverse('rtms_app:patient_first_visit', args=[patient.id])}?dashboard_date={dashboard_date}")
            return redirect('rtms_app:patient_first_visit', patient_id=patient.id)
    else:
        form = PatientBasicEditForm(instance=patient)

    return render(request, 'rtms_app/patient_basic_edit.html', {
        'patient': patient,
        'form': form,
        'dashboard_date': dashboard_date,
        'can_view_audit': can_view_audit(request.user),
    })


@login_required
def questionnaire_edit(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date')

    questions_past, questions_current, keys = _questionnaire_questions()
    questionnaire = patient.questionnaire_data or {}

    if request.method == 'POST':
        data = {}
        for k in keys:
            if k == 'q_details':
                continue
            v = (request.POST.get(k) or '').strip()
            data[k] = v if v in ('はい', 'いいえ') else 'いいえ'
        data['q_details'] = (request.POST.get('q_details') or '').strip()
        patient.questionnaire_data = data
        patient.save(update_fields=['questionnaire_data'])

        if dashboard_date:
            return redirect(f"{reverse('rtms_app:patient_first_visit', args=[patient.id])}?dashboard_date={dashboard_date}")
        return redirect('rtms_app:patient_first_visit', patient_id=patient.id)

    return render(request, 'rtms_app/questionnaire_edit.html', {
        'patient': patient,
        'dashboard_date': dashboard_date,
        'questionnaire': questionnaire,
        'questions_past': questions_past,
        'questions_current': questions_current,
        'can_view_audit': can_view_audit(request.user),
    })

@login_required
def sae_report_docx(request, session_id):
    """Generate and download SAE report as Word document."""
    from django.http import HttpResponse
    from .models import TreatmentSession, SeriousAdverseEvent
    from .services.sae_report import build_sae_context, render_sae_docx, get_missing_fields
    import os

    session = get_object_or_404(TreatmentSession, pk=session_id)
    patient = session.patient
    sae_record = SeriousAdverseEvent.objects.filter(patient=patient, session=session).first()

    if not sae_record:
        return HttpResponse("SAE record not found for this session.", status=404)

    # Build context
    context = build_sae_context(session, sae_record)
    missing = get_missing_fields(context)

    # Template path
    template_path = os.path.join(
        settings.BASE_DIR, "docs", "templates", "brainsway_sae_template.docx"
    )
    if not os.path.exists(template_path):
        return HttpResponse("SAE report template not found.", status=500)

    # Render docx
    try:
        docx_bytes = render_sae_docx(template_path, context)
    except Exception as e:
        return HttpResponse(f"Failed to render SAE report: {e}", status=500)

    # Return as download
    response = HttpResponse(docx_bytes, content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    response["Content-Disposition"] = f'attachment; filename="SAE_Report_{patient.card_id}_{session.session_date}.docx"'
    return response


@login_required
def adverse_event_report_print_preview(request):
    """Generate print preview for adverse event report."""
    from datetime import date
    patient_id = request.POST.get('patient_id')
    session_id = request.POST.get('session_id')
    patient = Patient.objects.filter(pk=patient_id).first() if patient_id else None
    session = TreatmentSession.objects.filter(pk=session_id).first() if session_id else None
    course_number = getattr(patient, 'course_number', None) or getattr(session, 'course_number', None)
    session_date = getattr(session, 'session_date', None)
    week_num = get_current_week_number(patient.first_treatment_date, session_date) if patient and session_date else None

    contact_person = resolve_contact_person(patient, session, request.user)
    contact_missing = not bool(contact_person)
    facility_name = "笠寺精治寮病院"
    facility_phone = "052-821-1229"

    age = request.POST.get('age') or (patient.age if patient else '')
    gender = request.POST.get('gender') or (patient.get_gender_display() if patient else '')
    initials = request.POST.get('initials') or compute_initials_from_name(getattr(patient, 'name', ''))
    diagnosis = request.POST.get('diagnosis') or getattr(patient, 'diagnosis', '')
    concomitant_meds = request.POST.get('concomitant_meds') or getattr(patient, 'medication_history', '')
    substance_use = request.POST.get('substance_use') or build_substance_use_summary(session) or 'なし'
    rmt_value = request.POST.get('rmt') or get_latest_resting_mt(patient, course_number, session_date, week_num)
    intensity_value = request.POST.get('intensity') or (getattr(session, 'mt_percent', None) or getattr(session, 'intensity_percent', None) or '')
    site_value = request.POST.get('site') or getattr(session, 'target_site', '')
    treatment_number_value = request.POST.get('treatment_number') or get_daily_treatment_number(patient, course_number, session_date) or ''
    onset_date_value = request.POST.get('onset_date') or (session_date.isoformat() if session_date else '')

    context = {
        'checked_events': request.POST.getlist('checked_events[]', []),
        'event_name': request.POST.get('event_name', ''),
        'onset_date': onset_date_value,
        'age': age,
        'gender': gender,
        'initials': initials,
        'diagnosis': diagnosis,
        'concomitant_meds': concomitant_meds,
        'substance_use': substance_use,
        'seizure_history': request.POST.get('seizure_history', ''),
        'seizure_history_detail': request.POST.get('seizure_history_detail', ''),
        'rmt': rmt_value or '',
        'intensity': intensity_value,
        'site': site_value,
        'treatment_number': treatment_number_value,
        'outcome': request.POST.get('outcome', ''),
        'outcome_sequelae': request.POST.get('outcome_sequelae', ''),
        'outcome_date': request.POST.get('outcome_date', ''),
        'notes': request.POST.get('notes', ''),
        'doctor_comment': request.POST.get('doctor_comment', ''),
        'report_date': date.today().strftime('%Y年%m月%d日'),
        'facility_name': facility_name,
        'facility_phone': facility_phone,
        'contact_person': contact_person,
        'contact_missing': contact_missing,
    }

    return render(request, 'rtms_app/print/adverse_event_report.html', context)


@login_required
def adverse_event_report_form(request, session_id):
    """GET/POST modal for editing adverse event report."""
    from .models import AdverseEventReport
    from datetime import date as dt_date
    
    session = get_object_or_404(TreatmentSession, pk=session_id)
    patient = session.patient
    course_number = patient.course_number or 1
    
    existing_report = AdverseEventReport.objects.filter(session=session).first()
    
    # Build prefill/initial data
    age_at_event = None
    if session.session_date and patient.birth_date:
        today_for_calc = session.session_date
        age_at_event = today_for_calc.year - patient.birth_date.year - (
            (today_for_calc.month, today_for_calc.day) < (patient.birth_date.month, patient.birth_date.day)
        )
    
    treatment_no = get_cumulative_treatment_number(patient, course_number, session_id)
    mapping_rmt = get_latest_resting_mt(patient, course_number, session.session_date, None)
    romaji_initials = convert_to_romaji_initials(patient.name)
    
    # Determine diagnosis default (うつ病 → うつ病エピソード)
    diagnosis_default = 'depressive_episode'  # default
    if 'うつ病' in (patient.diagnosis or ''):
        diagnosis_default = 'depressive_episode'
    
    prefill = {
        'age': age_at_event or '',
        'sex': patient.get_gender_display(),
        'initials': romaji_initials,
        'diagnosis': diagnosis_default,
        'concomitant_meds': patient.medication_history or '',
        'substance_use': build_substance_use_summary(session) or 'なし',
        'rmt': mapping_rmt or '',
        'intensity': session.mt_percent or session.intensity_percent or '',
        'site': session.target_site or '左DLPFC',
        'treatment_number': treatment_no or '',
        'onset_date': session.session_date.isoformat() if session.session_date else '',
        'contact_person': resolve_contact_person(patient, session, request.user),
    }
    
    if request.method == 'GET':
        # Render prefilled form HTML
        context = {
            'session': session,
            'patient': patient,
            'existing_report': existing_report,
            'prefill': prefill,
            'facility_name': '笠寺精治寮病院',
            'facility_phone': '052-821-1229',
        }
        return render(request, 'rtms_app/adverse_event_report_form.html', context)
    
    elif request.method == 'POST':
        # Save to database
        from .models import AdverseEventReport
        
        event_types_raw = request.POST.getlist('event_types', [])
        event_types = [e for e in event_types_raw if e]
        
        defaults = {
            'adverse_event_name': request.POST.get('event_name', '').strip(),
            'onset_date': request.POST.get('onset_date') or None,
            'age': int(request.POST.get('age')) if request.POST.get('age', '').isdigit() else None,
            'sex': request.POST.get('gender', ''),
            'initials': request.POST.get('initials', ''),
            'diagnosis_category': request.POST.get('diagnosis', 'depressive_episode'),
            'diagnosis_other_text': request.POST.get('diagnosis_other', ''),
            'concomitant_meds_text': request.POST.get('concomitant_meds', ''),
            'substance_intake_text': request.POST.get('substance_use', ''),
            'seizure_history_flag': request.POST.get('seizure_history') != '0',
            'seizure_history_date_text': request.POST.get('seizure_history_detail', ''),
            'rmt_value': int(request.POST.get('rmt')) if request.POST.get('rmt', '').replace('.', '', 1).isdigit() else None,
            'intensity_value': int(request.POST.get('intensity')) if request.POST.get('intensity', '').replace('.', '', 1).isdigit() else None,
            'stimulation_site_category': request.POST.get('site', 'left_dlpfc'),
            'stimulation_site_other_text': '',
            'treatment_course_number': treatment_no,
            'outcome_flags': event_types,
            'outcome_sequelae_text': '',
            'outcome_date': request.POST.get('outcome_date') or None,
            'special_notes': request.POST.get('notes', ''),
            'physician_comment': request.POST.get('doctor_comment', ''),
            'event_types': event_types,
            'prefilled_snapshot': {
                'patient_age': age_at_event,
                'cumulative_treatment_no': treatment_no,
                'mapping_rmt': mapping_rmt,
            },
        }
        
        report, created = AdverseEventReport.objects.update_or_create(
            session=session,
            defaults=defaults
        )
        
        # Return JSON with success and redirect URL
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            print_url = reverse('rtms_app:adverse_event_report_print', args=[session.id])
            return JsonResponse({
                'status': 'success',
                'message': 'Report saved successfully',
                'print_url': print_url,
            })
        
        # Redirect to print preview
        return redirect('rtms_app:adverse_event_report_print', session_id=session.id)


@login_required
def adverse_event_report_print(request, session_id):
    """Print preview for saved adverse event report."""
    session = get_object_or_404(TreatmentSession, pk=session_id)
    patient = session.patient
    report = get_object_or_404(AdverseEventReport, session=session)
    
    facility_name = '笠寺精治寮病院'
    facility_phone = '052-821-1229'
    contact_person = resolve_contact_person(patient, session, request.user)
    contact_missing = not bool(contact_person)
    
    context = {
        'report': report,
        'patient': patient,
        'session': session,
        'facility_name': facility_name,
        'facility_phone': facility_phone,
        'contact_person': contact_person,
        'contact_missing': contact_missing,
        'report_date': dt_date.today().strftime('%Y年%m月%d日'),
    }
    
    return render(request, 'rtms_app/print/adverse_event_report_db.html', context)


@login_required
def treatment_add(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id); dashboard_date = request.GET.get('dashboard_date')
    course_number = patient.course_number or 1
    target_date_str = request.GET.get('date'); now = timezone.localtime(timezone.now())
    if target_date_str:
        t = parse_date(target_date_str)
        initial_date = t or now.date()
    else: initial_date = now.date()
    # Session number derived from centralized planned schedule (唯一の正)
    session_num = None
    
    # Calculate week_no with day-based rollover anchored to first treatment date
    week_num = 1
    current_week_mapping = None
    mapping_alert = None

    # 画面表示用：治療予定サマリ（独立バー廃止の代替）
    plan_summary_text = None
    plan_date_range_text = None
    course_session_text = None

    # 上部患者バー右側：モード切替（治療画面専用）
    mode_switch_html = mark_safe(
        """
        <div class=\"btn-group\" role=\"group\" aria-label=\"mode-switch\">
            <input type=\"radio\" class=\"btn-check\" name=\"modeSwitch\" id=\"treatModeRecord\" autocomplete=\"off\" checked>
            <label class=\"btn btn-success btn-sm\" for=\"treatModeRecord\">
                <i class=\"fas fa-pen me-1\"></i>治療内容記入
            </label>

            <input type=\"radio\" class=\"btn-check\" name=\"modeSwitch\" id=\"treatModeWizard\" autocomplete=\"off\">
            <label class=\"btn btn-outline-success btn-sm\" for=\"treatModeWizard\">
                <i class=\"fas fa-route me-1\"></i>手順解説
            </label>
        </div>
        """
    )

    if patient.first_treatment_date:
        tdates = generate_treatment_dates(patient.first_treatment_date, total=30, holidays=JP_HOLIDAYS)
        if initial_date in tdates:
            idx = tdates.index(initial_date)
            week_num = get_current_week_number(patient.first_treatment_date, initial_date)
            session_num = idx + 1
        plan_start_date = patient.first_treatment_date
        plan_end_date = get_completion_date(patient.first_treatment_date)
        total_planned_sessions = len(tdates) if tdates else 30
    else:
        plan_start_date = None
        plan_end_date = None
        total_planned_sessions = 30

    if plan_start_date and plan_end_date and session_num and week_num:
        plan_date_range_text = (
            f"治療予定：{plan_start_date.strftime('%Y/%m/%d')}〜{plan_end_date.strftime('%Y/%m/%d')}"
        )
        course_session_text = f"{course_number}クール第{session_num}回（第{week_num}週）"
        # Backward-compatible combined form (used by older template fragments)
        plan_summary_text = f"{plan_date_range_text}｜{course_session_text}"
    
    # Fetch current week mapping: same date first, then same week
    same_date_mapping = MappingSession.objects.filter(patient=patient, course_number=course_number, date=initial_date).first()
    if same_date_mapping:
        current_week_mapping = same_date_mapping
    else:
        # Try to get mapping for current week_number
        current_week_mapping = MappingSession.objects.filter(patient=patient, course_number=course_number, week_number=week_num).order_by('-date').first()
    
    end_date_est = get_completion_date(patient.first_treatment_date)
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
            cleaned = form.cleaned_data
            d = cleaned['treatment_date']; t = cleaned['treatment_time']
            dt = datetime.datetime.combine(d, t); aware_dt = timezone.make_aware(dt)
            course_number = patient.course_number or 1
            session_date = d
            slot = ''
            # Check safety conditions for warning
            safety_sleep = cleaned.get('safety_sleep', True)
            safety_alcohol = cleaned.get('safety_alcohol', True)
            safety_meds = cleaned.get('safety_meds', True)
            
            defaults = {
                'date': aware_dt,
                'safety_sleep': safety_sleep,
                'safety_alcohol': safety_alcohol,
                'safety_meds': safety_meds,
                'coil_type': cleaned.get('coil_type', ''),
                'target_site': cleaned.get('target_site', ''),
                'mt_percent': cleaned.get('mt_percent'),
                'frequency_hz': cleaned.get('frequency_hz'),
                'train_seconds': cleaned.get('train_seconds'),
                'intertrain_seconds': cleaned.get('intertrain_seconds'),
                'train_count': cleaned.get('train_count'),
                'helmet_shift_cm': cleaned.get('helmet_shift_cm'),
                'total_pulses': cleaned.get('total_pulses'),
                'treatment_notes': cleaned.get('treatment_notes',''),
                'motor_threshold': cleaned.get('mt_percent'),
                'intensity': cleaned.get('intensity_percent'),
                'performer': request.user,
                'course_number': course_number,
                'session_date': session_date,
                'slot': slot,
            }
            s, created = TreatmentSession.objects.update_or_create(
                patient=patient,
                course_number=course_number,
                session_date=session_date,
                slot=slot,
                defaults=defaults
            )

            # Save Step6 confirm fields/flags and Step7 treat fields into meta
            try:
                meta = s.meta or {}
                cps = (request.POST.get('confirm_pulse_seconds') or '').strip()
                cmp = (request.POST.get('confirm_mt_percent') or '').strip()
                cn = (request.POST.get('confirm_notes') or '').strip()
                cd = request.POST.get('confirm_discomfort')
                cm = request.POST.get('confirm_movement')
                tn = (request.POST.get('treat_notes') or '').strip()
                td = request.POST.get('treat_discomfort')
                tm = request.POST.get('treat_movement')
                
                if cps:
                    try:
                        meta['confirm_pulse_seconds'] = float(cps)
                    except Exception:
                        pass
                if cmp:
                    try:
                        meta['confirm_mt_percent'] = int(cmp)
                    except Exception:
                        pass
                if cn:
                    meta['confirm_notes'] = cn
                
                # チェックボックスはPOSTに含まれる場合のみTrue、含まれない場合はFalse
                meta['confirm_discomfort'] = (cd == 'on')
                meta['confirm_movement'] = (cm == 'on')
                if tn:
                    meta['treat_notes'] = tn
                meta['treat_discomfort'] = (td == 'on')
                meta['treat_movement'] = (tm == 'on')
                
                if meta != (s.meta or {}):
                    s.meta = meta
                    s.save(update_fields=['meta'])
            except Exception:
                pass

            # Upsert SideEffectCheck linked to this session
            rows_json = request.POST.get('side_effect_rows_json')
            signature = request.POST.get('side_effect_signature', '')
            memo = request.POST.get('side_effect_memo', '')
            try:
                rows = json.loads(rows_json) if rows_json else []
            except Exception:
                rows = []

            sec, created = SideEffectCheck.objects.get_or_create(session=s)
            sec.rows = rows or []
            # Save provided memo and signature
            sec.memo = memo or sec.memo or ""
            sec.physician_signature = signature or sec.physician_signature or ""
            sec.save()

            # Consolidate side-effect memo into session meta so UI-unified memo appears
            try:
                smemo = sec.memo or ""
                if smemo:
                    meta = s.meta or {}
                    # keep legacy keys for backward compatibility but set from unified memo
                    meta['confirm_notes'] = smemo
                    meta['treat_notes'] = smemo
                    s.meta = meta
                    s.save(update_fields=['meta'])
            except Exception:
                pass

            # Upsert SeriousAdverseEvent if any SAE checkbox is checked
            from .models import SeriousAdverseEvent
            sae_fields = ['sae_seizure', 'sae_finger_muscle', 'sae_syncope', 'sae_mania', 'sae_suicide_attempt', 'sae_other']
            sae_event_types = []
            sae_map = {
                'sae_seizure': 'seizure',
                'sae_finger_muscle': 'finger_muscle',
                'sae_syncope': 'syncope',
                'sae_mania': 'mania',
                'sae_suicide_attempt': 'suicide_attempt',
                'sae_other': 'other',
            }
            for f in sae_fields:
                if request.POST.get(f) == 'on':
                    sae_event_types.append(sae_map[f])
            
            sae_other_text = (request.POST.get('sae_other_text') or '').strip()

            if sae_event_types:
                # Build snapshot: key info at time of SAE
                try:
                    snapshot = {
                        'date': session_date.isoformat(),
                        'mt_percent': s.mt_percent,
                        'frequency_hz': str(s.frequency_hz),
                        'train_seconds': str(s.train_seconds),
                        'train_count': s.train_count,
                        'total_pulses': s.total_pulses,
                        'coil_type': s.coil_type,
                        'target_site': s.target_site,
                        'diagnosis': patient.diagnosis,
                        # Medication snapshot (placeholder, adjust for your medication model)
                        'medication_history': patient.medication_history,
                        'age': patient.age,
                        'gender': patient.get_gender_display(),
                    }
                except Exception:
                    snapshot = {}

                SeriousAdverseEvent.objects.update_or_create(
                    patient=patient,
                    course_number=course_number,
                    session=s,
                    defaults={
                        'event_types': sae_event_types,
                        'other_text': sae_other_text,
                        'auto_snapshot': snapshot,
                    }
                )
            else:
                # No SAE events checked: delete existing record if any
                SeriousAdverseEvent.objects.filter(patient=patient, course_number=course_number, session=s).delete()
            
            # Check if print action is requested
            action = request.POST.get('action')
            if action == 'print':
                # Redirect to print page (PRG) with explicit back_url
                back_params = {'date': d.isoformat()}
                if dashboard_date:
                    back_params['dashboard_date'] = dashboard_date
                back_url = f"{reverse('rtms_app:treatment_add', args=[patient.id])}?{urlencode(back_params)}"

                print_params = {'back_url': back_url}
                if dashboard_date:
                    print_params['dashboard_date'] = dashboard_date
                print_url = f"{reverse('rtms_app:print:print_side_effect_check', args=[patient.id, s.id])}?{urlencode(print_params)}"
                return redirect(print_url)
            
            # Wizard action or normal save
            if action == 'save_from_wizard':
                # ウィザードから保存：成功した旨をJSONで返す or 通常リダイレクト
                # Redirect back to dashboard, include focus for client-side calendar handling
                focus_date = session_date.isoformat()
                if dashboard_date:
                    url = build_url('dashboard', query={'date': dashboard_date, 'focus': focus_date})
                else:
                    url = build_url('dashboard', query={'focus': focus_date})
                if settings.DEBUG:
                    logging.getLogger(__name__).debug(f"treatment_add.save_from_wizard POST date={d} saved={s.session_date} redirect={url}")
                return redirect(url)

            # Skip action: mark this session as skipped and shift future planned sessions
            if action == 'skip':
                try:
                    s.status = 'skipped'
                    s.save(update_fields=['status'])
                except Exception:
                    pass
                try:
                    shift_future_sessions(patient, session_date)
                except Exception:
                    pass

                # redirect back to dashboard with focus on the skipped date
                focus_date = session_date.isoformat()
                if dashboard_date:
                    url = build_url('dashboard', query={'date': dashboard_date, 'focus': focus_date})
                else:
                    url = build_url('dashboard', query={'focus': focus_date})
                if settings.DEBUG:
                    import logging
                    logging.getLogger(__name__).debug(f"treatment_add.skip POST date={d} skipped session={s.id} redirect={url}")
                return redirect(url)

            # Normal save -> go back to dashboard (include focus param so client calendar can jump)
            focus_date = session_date.isoformat()
            if dashboard_date:
                url = build_url('dashboard', query={'date': dashboard_date, 'focus': focus_date})
            else:
                url = build_url('dashboard', query={'focus': focus_date})
            if settings.DEBUG:
                logging.getLogger(__name__).debug(f"treatment_add.save POST date={d} saved={s.session_date} redirect={url}")
            return redirect(url)
        else:
            pass
    else:
        # GET request - setup form with initial data
        initial_data = {'treatment_date': initial_date, 'treatment_time': now.strftime('%H:%M'), 'total_pulses': 1980}
        
        # Use mapping data if available
        if current_week_mapping: 
            # initial_data['mt_percent'] = current_week_mapping.resting_mt
            initial_data['mt_percent'] = 120
        
        form = TreatmentForm(initial=initial_data)
    
    # Load existing session and side effect data (DB is the source of truth)
    side_effect_rows = []
    side_effect_signature = ''
    side_effect_memo = ''
    existing_session = None
    
    # Find a treatment session for this patient on the initial_date using session_date field
    course_number = patient.course_number or 1
    existing_session = TreatmentSession.objects.filter(
        patient=patient,
        course_number=course_number,
        session_date=initial_date
    ).order_by('-date').first()
    
    if existing_session:
        # Populate form with existing session data (only if GET request)
        if request.method == 'GET':
            form = TreatmentForm(initial={
                'treatment_date': existing_session.date.date(),
                'treatment_time': existing_session.date.strftime('%H:%M'),
                'safety_sleep': existing_session.safety_sleep,
                'safety_alcohol': existing_session.safety_alcohol,
                'safety_meds': existing_session.safety_meds,
                'coil_type': existing_session.coil_type,
                'target_site': existing_session.target_site,
                'mt_percent': existing_session.mt_percent,
                'frequency_hz': existing_session.frequency_hz,
                'train_seconds': existing_session.train_seconds,
                'intertrain_seconds': existing_session.intertrain_seconds,
                'train_count': existing_session.train_count,
                'total_pulses': existing_session.total_pulses,
                'helmet_shift_cm': getattr(existing_session, 'helmet_shift_cm', None),
                'treatment_notes': existing_session.treatment_notes or '',
            })
        
        # Load side effect data
        sec = SideEffectCheck.objects.filter(session=existing_session).first()
        if sec:
            side_effect_rows = sec.rows or []
            side_effect_signature = sec.physician_signature or ''
            side_effect_memo = sec.memo or ''

    # If no SideEffectCheck exists for this session/date, keep an empty array;
    # the widget renders default rows client-side.

    side_effect_rows_json = json.dumps(side_effect_rows, ensure_ascii=False)

    # 印刷プレビューURL（既存セッションがある場合のみ）
    side_effect_print_url = None
    try:
        if existing_session:
            back_params = {'date': initial_date.isoformat()}
            if dashboard_date:
                back_params['dashboard_date'] = dashboard_date
            back_url = f"{reverse('rtms_app:treatment_add', args=[patient.id])}?{urlencode(back_params)}"

            print_params = {'back_url': back_url}
            if dashboard_date:
                print_params['dashboard_date'] = dashboard_date
            side_effect_print_url = (
                f"{reverse('rtms_app:print:print_side_effect_check', args=[patient.id, existing_session.id])}?{urlencode(print_params)}"
            )
    except Exception:
        side_effect_print_url = None

    # Build Step6 previous abnormal alert message (latest within recent 5 sessions)
    confirm_alert_message = ''
    try:
        prev_qs = TreatmentSession.objects.filter(
            patient=patient,
            course_number=course_number,
            session_date__lt=initial_date
        ).order_by('-session_date')[:5]
        alert_session = None
        for ts in prev_qs:
            m = getattr(ts, 'meta', None) or {}
            if m.get('confirm_discomfort') or m.get('confirm_movement'):
                alert_session = ts
                break
        if alert_session:
            m = alert_session.meta or {}
            d = alert_session.session_date
            # compute session number within plan
            sess_no = get_session_number(patient.first_treatment_date, d) if patient.first_treatment_date else None
            d_str = f"{d.month}月{d.day}日"
            sec = m.get('confirm_pulse_seconds')
            pct = m.get('confirm_mt_percent')
            kinds = []
            if m.get('confirm_discomfort'):
                kinds.append('不快感')
            if m.get('confirm_movement'):
                kinds.append('運動反応')
            kinds_label = '・'.join(kinds) if kinds else ''
            if sec is not None and pct is not None and kinds_label:
                if sess_no and sess_no > 0:
                    confirm_alert_message = f"{d_str}（第{sess_no}回）には、刺激時間{sec}秒、刺激強度{pct}%MTで、過剰な{kinds_label}がありました。"
                else:
                    confirm_alert_message = f"{d_str}には、刺激時間{sec}秒、刺激強度{pct}%MTで、過剰な{kinds_label}がありました。"
    except Exception:
        confirm_alert_message = ''
    # Side effect items for template
    side_effect_items = [
        ('headache', '頭痛'),
        ('scalp_pain', '頭皮痛'),
        ('neck_pain', '頸部痛'),
        ('facial_twitch', '顔面けいれん'),
        ('dizziness', 'めまい'),
        ('nausea', '吐き気/嘔吐'),
        ('hearing_change', '聴力変化'),
        ('ear_ringing', '耳鳴り'),
        ('mood_change', '気分変化'),
        ('memory_issue', '記憶障害/混乱'),
        ('muscle_pain', '筋肉痛'),
        ('seizure_risk', '発作'),
    ]

    # Build print_options for floating menu
    print_options = []

    
    # HAMD 第3週評価の構造化情報
    hamd3w_available = last_assessment is not None
    hamd3w_score = getattr(last_assessment, 'total_score_17', None) if last_assessment else None
    hamd3w_improvement_pct = None
    hamd3w_status = None
    if last_assessment and baseline_assessment and baseline_assessment.total_score_17 not in (None, 0):
        hamd3w_improvement_pct = round((baseline_assessment.total_score_17 - last_assessment.total_score_17) / baseline_assessment.total_score_17 * 100.0, 1)
        hamd3w_status = classify_hamd_response(hamd3w_score, hamd3w_improvement_pct)
    
    hamd_eval = {
        'score_17': hamd3w_score,
        'improvement_pct': hamd3w_improvement_pct,
        'status_label': hamd3w_status,
        'instruction': instruction_msg or None,
    }
    
    # 位置決めが必要かどうかの判定
    needs_mapping_today = False
    mapping_reason = None
    mapping_done = current_week_mapping is not None
    
    # 判定条件1: 週の初回セッション
    if session_num == 1 or (session_num and session_num % 5 == 1):
        needs_mapping_today = True
        mapping_reason = "週の第1回目の治療のため"
    
    # 判定条件2: 安全確認が1つでもOFFの場合（POSTデータがあれば参照、なければ既存セッションデータ）
    if request.method == 'POST':
        safety_sleep = request.POST.get('safety_sleep') == 'on'
        safety_alcohol = request.POST.get('safety_alcohol') == 'on'
        safety_meds = request.POST.get('safety_meds') == 'on'
    elif existing_session:
        safety_sleep = existing_session.safety_sleep
        safety_alcohol = existing_session.safety_alcohol
        safety_meds = existing_session.safety_meds
    else:
        safety_sleep = True
        safety_alcohol = True
        safety_meds = True
    
    if not (safety_sleep and safety_alcohol and safety_meds):
        needs_mapping_today = True
        if mapping_reason:
            mapping_reason += " / 安全確認で要再測定の項目があります"
        else:
            mapping_reason = "安全確認で要再測定の項目があります"

    # 治療画面UI用：今週の位置決めが未設定 or 安全確認がOFFなら「位置決めをしてください」を表示
    need_mapping = (current_week_mapping is None) or (not (safety_sleep and safety_alcohol and safety_meds))
    
    # 位置決めURL
    # Include the current initial_date when linking to mapping_add so the mapping form
    # opens for the same session date (allows editing existing mapping on that date).
    mapping_url = reverse('rtms_app:mapping_add', args=[patient.id])
    mapping_query = {'date': initial_date.isoformat()}
    if dashboard_date:
        mapping_query['dashboard_date'] = dashboard_date
    mapping_url = build_url('mapping_add', args=[patient.id], query=mapping_query)
    
    # 確認刺激のデフォルト値
    confirm_defaults = {
        'seconds': 2.0,
        'percent': 120,
    }
    
    # 確認刺激の治療刺激のチェックボックス初期値（既存セッションから）
    confirm_discomfort_checked = False
    confirm_movement_checked = False
    treat_discomfort_checked = False
    treat_movement_checked = False
    confirm_notes = ''
    treat_notes = ''
    if existing_session and existing_session.meta:
        m = existing_session.meta
        confirm_defaults['seconds'] = m.get('confirm_pulse_seconds', 2.0)
        confirm_defaults['percent'] = m.get('confirm_mt_percent', 120)
        confirm_discomfort_checked = m.get('confirm_discomfort', False)
        confirm_movement_checked = m.get('confirm_movement', False)
        treat_discomfort_checked = m.get('treat_discomfort', False)
        treat_movement_checked = m.get('treat_movement', False)
        confirm_notes = m.get('confirm_notes', '')
        treat_notes = m.get('treat_notes', '')

    # 既存SAEデータの読み込み
    existing_sae = None
    sae_event_types_checked = {}
    sae_other_text_value = ''
    if existing_session:
        from .models import SeriousAdverseEvent
        existing_sae = SeriousAdverseEvent.objects.filter(
            patient=patient, 
            course_number=course_number, 
            session=existing_session
        ).first()
        if existing_sae:
            # チェックボックスの状態を復元
            for event_code in existing_sae.event_types:
                sae_event_types_checked[f'sae_{event_code}'] = True
            sae_other_text_value = existing_sae.other_text or ''

    # Prefill values for adverse event report modal
    mapping_rmt_value = current_week_mapping.resting_mt if current_week_mapping else None
    session_rmt_value = getattr(existing_session, 'motor_threshold', None) if existing_session else None
    intensity_value = None
    if existing_session:
        intensity_value = existing_session.mt_percent or existing_session.intensity_percent
    site_value = ''
    if existing_session and existing_session.target_site:
        site_value = existing_session.target_site
    elif current_week_mapping and current_week_mapping.stimulation_site:
        site_value = current_week_mapping.stimulation_site
    else:
        site_value = '左DLPFC'

    onset_date_prefill = existing_session.session_date.isoformat() if existing_session else initial_date.isoformat()
    treatment_number_today = session_num or get_cumulative_treatment_number(patient, course_number, existing_session.id if existing_session else None) or ''
    contact_person_prefill = resolve_contact_person(patient, existing_session, request.user)
    sae_prefill = {
        'age': patient.age,
        'gender': patient.get_gender_display(),
        'initials': compute_initials_from_name(patient.name),
        'diagnosis': patient.diagnosis or '',
        'concomitant_meds': patient.medication_history or '',
        'substance_use': build_substance_use_summary(existing_session) if existing_session else '飲酒/過量カフェイン: 摂取なし / 薬剤変更なし',
        'rmt': mapping_rmt_value or session_rmt_value or '',
        'intensity': intensity_value or '',
        'site': site_value,
        'treatment_number': treatment_number_today,
        'onset_date': onset_date_prefill,
        'contact_person': contact_person_prefill,
    }

    return render(request, 'rtms_app/treatment_add.html', {
        'patient': patient,
        'form': form,
        'session': existing_session,  # SAEダウンロード用
        'current_week_mapping': current_week_mapping,
        'mapping_alert': mapping_alert,
        'initial_date': initial_date,
        'session_num': session_num,
        'week_num': week_num,
        'end_date_est': end_date_est,
        'start_date': patient.first_treatment_date,
        'dashboard_date': dashboard_date,
        'alert_msg': alert_msg,
        'instruction_msg': instruction_msg,
        'judgment_info': judgment_info,
        'side_effect_items': side_effect_items,
        'side_effect_rows_json': side_effect_rows_json,
        'side_effect_signature': side_effect_signature,
        'side_effect_memo': side_effect_memo,
        'print_options': print_options,
        'today': timezone.now().date(),
        'initial_timing_display': '',
        'can_view_audit': can_view_audit(request.user),
        # Treatment header/controls
        'plan_summary_text': plan_summary_text,
        'plan_date_range_text': plan_date_range_text,
        'course_session_text': course_session_text,
        'mode_switch_html': mode_switch_html,
        'side_effect_print_url': side_effect_print_url,
        # Summary bar (both legacy names and new unified names)
        'plan_start_date': plan_start_date,
        'plan_end_date': plan_end_date,
        'today_session_no': session_num,
        'total_planned_sessions': total_planned_sessions,
        'week_num': week_num,
        # Unified names for plan_inline_bar.html partial
        'treatment_plan_start': plan_start_date,
        'treatment_plan_end': plan_end_date,
        'total_sessions': total_planned_sessions,
        'week_no': week_num,
        # HAMD instruction card
        'hamd_eval': hamd_eval,
        'hamd3w_available': hamd3w_available,
        'hamd3w_score': hamd3w_score,
        'hamd3w_improvement_pct': hamd3w_improvement_pct,
        'hamd3w_status': hamd3w_status,
        # Wizard Step6 previous abnormal alert
        'confirm_alert_message': confirm_alert_message,
        # Mapping alert
        'needs_mapping_today': needs_mapping_today,
        'need_mapping': need_mapping,
        'mapping_done': mapping_done,
        'mapping_reason': mapping_reason,
        'mapping_url': mapping_url,
        # Confirm stimulus defaults
        'confirm_defaults': confirm_defaults,
        'confirm_discomfort_checked': confirm_discomfort_checked,
        'confirm_movement_checked': confirm_movement_checked,
        'treat_discomfort_checked': treat_discomfort_checked,
        'treat_movement_checked': treat_movement_checked,
        'confirm_notes': confirm_notes,
        'treat_notes': treat_notes,
        # SAE data
        'existing_sae': existing_sae,
        'sae_event_types_checked': sae_event_types_checked,
        'sae_other_text_value': sae_other_text_value,
        'sae_prefill': sae_prefill,
        'sae_contact_person': contact_person_prefill,
        'sae_contact_missing': not bool(contact_person_prefill),
    })

def assessment_add(request, patient_id, timing):
    # Legacy endpoint kept for backward compatibility.
    # Redirect to new hub while preserving query params.
    q = request.GET.dict()
    return redirect(build_url('assessment_hub', args=[patient_id, timing], query=q))

def assessment_week4(request, patient_id):
    """
    第4週目評価用のラッパービュー
    """
    q = request.GET.dict()
    return redirect(build_url('assessment_hub', args=[patient_id, 'week4'], query=q))


def _planned_discharge_date(patient):
    """Return planned discharge date (30th treatment + 1 day) if actual discharge is missing."""
    if patient.discharge_date:
        return patient.discharge_date
    if not patient.first_treatment_date:
        return None
    planned_dates = generate_treatment_dates(patient.first_treatment_date, total=30, holidays=JP_HOLIDAYS)
    if not planned_dates:
        return None
    return planned_dates[-1] + timedelta(days=1)


def _build_month_calendar(year: int, month: int, is_print: bool = False):
    MAX_EVENTS_PRINT = 3
    MAX_EVENTS_SCREEN = 6
    MAX_EVENTS_PER_DAY = MAX_EVENTS_PRINT if is_print else MAX_EVENTS_SCREEN
    today = timezone.localdate()
    first_day = date(year, month, 1)
    last_day = date(year, month, monthrange(year, month)[1])

    # Grid start/end (Mon-Sun)
    grid_start = first_day - timedelta(days=first_day.weekday())
    grid_end = last_day + timedelta(days=(6 - last_day.weekday()))

    # All sessions in range (for counts + per-day events)
    sessions_qs = (
        TreatmentSession.objects
        .filter(session_date__range=[grid_start, grid_end])
        .select_related('patient', 'performer')
        .order_by('patient_id', 'course_number', 'session_date', 'date', 'id')
    )

    # Assign session numbers per patient+course in chronological order
    session_numbers = {}
    last_key = None
    counter = 0
    for s in sessions_qs:
        key = (s.patient_id, s.course_number)
        if key != last_key:
            counter = 0
            last_key = key
        counter += 1
        session_numbers[s.id] = counter

    rtms_counts = defaultdict(int)
    day_treatment_events = defaultdict(list)
    actual_session_numbers = defaultdict(set)  # (pid, course) -> set of session numbers
    for s in sessions_qs:
        rtms_counts[s.session_date] += 1
        session_no = session_numbers.get(s.id, 0)
        actual_session_numbers[(s.patient_id, s.course_number)].add(session_no)
        day_treatment_events[s.session_date].append({
            'label': f"治療{session_no}回 {s.patient.name}",
            'kind': 'treatment',
            'patient_id': s.patient_id,
            'session_id': s.id,
            'url': build_url('treatment_add', [s.patient_id], {'date': s.session_date.isoformat()}),
            'is_planned': False,
            'sort_key': 30 + session_no,  # treatment order later
        })

    # Patients possibly overlapping this grid
    patients = Patient.objects.filter(
        Q(admission_date__isnull=False) | Q(first_treatment_date__isnull=False)
    )

    inpatient_counts = defaultdict(int)
    events_by_date = defaultdict(list)

    for p in patients:
        # 初診（登録日）
        first_visit = p.created_at.date() if hasattr(p, "created_at") and p.created_at else None
        if first_visit and grid_start <= first_visit <= grid_end:
            events_by_date[first_visit].append({
                'label': f"初診 {p.name}",
                'kind': 'first-visit',
                'patient_id': p.id,
                'url': build_url('patient_first_visit', [p.id])
            })

        planned_discharge = _planned_discharge_date(p)
        # Inpatient window
        if p.admission_date and planned_discharge:
            start = max(p.admission_date, grid_start)
            end_excl = min(planned_discharge, grid_end + timedelta(days=1))
            cur = start
            while cur < end_excl:
                inpatient_counts[cur] += 1
                cur += timedelta(days=1)

        # Events
        if p.admission_date and grid_start <= p.admission_date <= grid_end:
            events_by_date[p.admission_date].append({
                'label': f"入院 {p.name}",
                'kind': 'admission',
                'patient_id': p.id,
                'url': build_url('admission_procedure', [p.id])
            })

        # Planned treatments up to 30 (skip those already done)
        if p.first_treatment_date:
            planned_dates = generate_treatment_dates(p.first_treatment_date, total=30, holidays=JP_HOLIDAYS)
            actual_nos = actual_session_numbers.get((p.id, p.course_number), set())
            for idx, d in enumerate(planned_dates, start=1):
                if idx in actual_nos:
                    continue
                if grid_start <= d <= grid_end:
                    events_by_date[d].append({
                        'label': f"治療{idx}回 (予定) {p.name}",
                        'kind': 'treatment',
                        'patient_id': p.id,
                        'url': build_url('treatment_add', [p.id], {'date': d.isoformat()}),
                        'is_planned': True,
                        'sort_key': 30 + idx,
                    })

        if p.discharge_date and grid_start <= p.discharge_date <= grid_end:
            events_by_date[p.discharge_date].append({
                'label': f"退院 {p.name}",
                'kind': 'discharge',
                'patient_id': p.id,
                'url': build_url('patient_home', [p.id]),
                'is_planned': False,
                'sort_key': 20,
            })
        elif planned_discharge and grid_start <= planned_discharge <= grid_end:
            events_by_date[planned_discharge].append({
                'label': f"退院予定 {p.name}",
                'kind': 'discharge',
                'patient_id': p.id,
                'url': build_url('patient_home', [p.id]),
                'is_planned': True,
                'sort_key': 20,
            })

    # Build day cells
    days = []
    cur = grid_start
    while cur <= grid_end:
        day_events = events_by_date.get(cur, [])
        day_events.extend(day_treatment_events.get(cur, []))

        # Normalize to 4 kinds only
        normalized = []
        for ev in day_events:
            kind = ev.get('kind')
            is_planned = ev.get('is_planned', False)
            sort_key = ev.get('sort_key')
            if sort_key is None:
                if kind == 'admission':
                    sort_key = 10
                elif kind == 'discharge':
                    sort_key = 20
                elif kind == 'treatment':
                    sort_key = 30
                elif kind == 'first-visit':
                    sort_key = 90
                else:
                    sort_key = 99
            normalized.append({**ev, 'kind': kind, 'is_planned': is_planned, 'sort_key': sort_key})

        normalized.sort(key=lambda x: (x.get('sort_key', 99), x.get('label', '')))

        # Limit visible events
        visible = normalized[:MAX_EVENTS_PER_DAY]
        hidden_count = max(len(normalized) - MAX_EVENTS_PER_DAY, 0)

        # Check if holiday
        holiday_name = None
        is_holiday = False
        if jpholiday:
            holiday_name = jpholiday.is_holiday_name(cur)
            is_holiday = holiday_name is not None

        days.append({
            'date': cur,
            'is_current_month': cur.month == month,
            'weekday': cur.weekday(),
            'rtms_count': rtms_counts.get(cur, 0),
            'inpatient_count': inpatient_counts.get(cur, 0),
            'events_visible': visible,
            'events_hidden_count': hidden_count,
            'day_url': build_url('dashboard', query={'date': cur.isoformat()}),
            'is_holiday': is_holiday,
            'holiday_name': holiday_name,
        })
        cur += timedelta(days=1)

    weeks = []
    for i in range(0, len(days), 7):
        weeks.append(days[i:i+7])

    peak_rtms = max(rtms_counts.values()) if rtms_counts else 0
    peak_inpatients = max(inpatient_counts.values()) if inpatient_counts else 0

    prev_month_date = first_day - timedelta(days=1)
    next_month_date = last_day + timedelta(days=1)

    return {
        'year': year,
        'month': month,
        'first_day': first_day,
        'last_day': last_day,
        'grid_start': grid_start,
        'grid_end': grid_end,
        'weeks': weeks,
        'peak_rtms': peak_rtms,
        'peak_inpatients': peak_inpatients,
        'prev_year': prev_month_date.year,
        'prev_month': prev_month_date.month,
        'next_year': next_month_date.year,
        'next_month': next_month_date.month,
        'today': today,
    }


@login_required
def calendar_month_view(request):
    today = timezone.localdate()
    try:
        year = int(request.GET.get('year', today.year))
        month = int(request.GET.get('month', today.month))
        first_day = date(year, month, 1)  # validation
    except Exception:
        year = today.year
        month = today.month

    data = _build_month_calendar(year, month, is_print=False)
    return render(request, "rtms_app/calendar_month.html", data)


@login_required
def calendar_month_print_view(request):
    today = timezone.localdate()
    try:
        year = int(request.GET.get('year', today.year))
        month = int(request.GET.get('month', today.month))
        _ = date(year, month, 1)
    except Exception:
        year = today.year
        month = today.month

    data = _build_month_calendar(year, month, is_print=True)
    return render(request, "rtms_app/print/calendar_month.html", data)


@login_required
def assessment_hub(request, patient_id, timing):
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date')
    from_page = request.GET.get('from')

    allowed = [c[0] for c in Assessment.TIMING_CHOICES]
    if timing not in allowed:
        return HttpResponse(status=400)

    timing_display = dict(Assessment.TIMING_CHOICES).get(timing, timing)
    window_start, window_end = get_assessment_window(patient, timing)

    # Pass through selected date (calendar/dashboard) for first open.
    date_param = (
        request.GET.get('date')
        or request.GET.get('dashboard_date')
        or request.GET.get('selected_date')
        or request.GET.get('calendar_date')
    )

    configs = (
        TimingScaleConfig.objects.select_related('scale')
        .filter(timing=timing, is_enabled=True, scale__is_active=True)
        .order_by('display_order', 'scale__code')
    )
    if not configs.exists():
        hamd = ScaleDefinition.objects.filter(code='hamd').first()
        configs = []
        if hamd:
            class _C:  # minimal placeholder
                scale = hamd
            configs = [_C()]

    course_number = patient.course_number or 1
    scales = []
    for cfg in configs:
        scale = cfg.scale
        record = (
            AssessmentRecord.objects.filter(
                patient=patient,
                course_number=course_number,
                timing=timing,
                scale=scale,
            )
            .order_by('-date')
            .first()
        )

        legacy = None
        if scale.code == 'hamd':
            legacy = (
                Assessment.objects.filter(
                    patient=patient,
                    course_number=course_number,
                    timing=timing,
                    type='HAM-D',
                )
                .order_by('-date')
                .first()
            )

        existing = record or legacy
        query = {}
        if dashboard_date:
            query['dashboard_date'] = dashboard_date
        if from_page:
            query['from'] = from_page
        if date_param:
            query['date'] = date_param

        scales.append({
            'code': scale.code,
            'name': scale.name,
            'is_done': existing is not None,
            'date': getattr(existing, 'date', None),
            'url': build_url('assessment_scale', args=[patient.id, timing, scale.code], query=query or None),
        })

    return render(request, 'rtms_app/assessment/hub.html', {
        'patient': patient,
        'dashboard_date': dashboard_date,
        'initial_timing': timing,
        'initial_timing_display': timing_display,
        'window_start': window_start,
        'window_end': window_end,
        'scales': scales,
        'can_view_audit': can_view_audit(request.user),
    })


def assessment_hub_redirect(request, patient_id, initial_timing):
    """Redirect /assessment/hub/<initial_timing>/ to canonical assessment_hub.
    
    Compatibility shim: preserves querystring and maps initial_timing parameter.
    """
    qs = request.META.get('QUERY_STRING', '')
    target = reverse('rtms_app:assessment_hub', args=[patient_id, initial_timing])
    if qs:
        target = f"{target}?{qs}"
    return redirect(target)


@login_required
def assessment_scale_form(request, patient_id, timing, scale_code):
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date')
    from_page = request.GET.get('from')

    allowed = [c[0] for c in Assessment.TIMING_CHOICES]
    if timing not in allowed:
        return HttpResponse(status=400)

    scale = get_object_or_404(ScaleDefinition, code=scale_code)

    timing_display = dict(Assessment.TIMING_CHOICES).get(timing, timing)
    window_start, window_end = get_assessment_window(patient, timing)

    course_number = patient.course_number or 1

    record = (
        AssessmentRecord.objects.filter(
            patient=patient,
            course_number=course_number,
            timing=timing,
            scale=scale,
        )
        .order_by('-date')
        .first()
    )

    legacy = None
    if scale.code == 'hamd':
        legacy = (
            Assessment.objects.filter(
                patient=patient,
                course_number=course_number,
                timing=timing,
                type='HAM-D',
            )
            .order_by('-date')
            .first()
        )

    # Determine initial date priority
    initial_date = timezone.now().date()
    date_param = (
        request.GET.get('date')
        or request.GET.get('dashboard_date')
        or request.GET.get('selected_date')
        or request.GET.get('calendar_date')
    )
    if date_param:
        try:
            initial_date = datetime.datetime.strptime(date_param, '%Y-%m-%d').date()
        except Exception:
            pass

    existing_for_default = record or legacy
    if existing_for_default and not date_param:
        initial_date = existing_for_default.date

    default_date = initial_date

    if request.method == 'POST':
        try:
            date_str = (request.POST.get('date') or '').strip()
            try:
                assessed_date = datetime.date.fromisoformat(date_str) if date_str else default_date
            except Exception:
                assessed_date = default_date

            note = (request.POST.get('note') or '').strip()

            if scale.code != 'hamd':
                return HttpResponse(status=400)

            hamd_items, _left, _right = _hamd_items()
            scores = {}
            for key, _label, maxv, _text in hamd_items:
                v = request.POST.get(key, '0')
                try:
                    iv = int(v)
                except Exception:
                    iv = 0
                iv = max(0, min(iv, maxv))
                scores[key] = iv

            rec_defaults = {
                'date': assessed_date,
                'scores': scores,
                'note': note,
                'course_number': course_number,
            }

            # Calculate improvement/status for non-baseline
            if timing != 'baseline':
                from .assessment_rules import compute_improvement_rate, classify_response_status
                baseline_obj = Assessment.objects.filter(
                    patient=patient, course_number=course_number, timing='baseline', type='HAM-D'
                ).order_by('-date').first()
                if baseline_obj:
                    baseline_17 = baseline_obj.total_score_17
                else:
                    baseline_17 = None

                # Compute improvement
                keys17 = [f"q{i}" for i in range(1, 18)]
                current_17 = sum(int(scores.get(k, 0)) for k in keys17)
                improv_rate = compute_improvement_rate(baseline_17, current_17)
                status = classify_response_status(current_17, improv_rate)

                rec_defaults['improvement_rate_17'] = improv_rate
                rec_defaults['status_label'] = status

            new_record, _created = AssessmentRecord.objects.update_or_create(
                patient=patient,
                course_number=course_number,
                timing=timing,
                scale=scale,
                defaults=rec_defaults,
            )

            # Keep legacy table in sync (so existing dashboards/flows continue to work)
            if scale.code == 'hamd':
                legacy_defaults = {
                    'date': assessed_date,
                    'scores': scores,
                    'note': note,
                    'type': 'HAM-D',
                    'course_number': course_number,
                }
                legacy_obj, _legacy_created = Assessment.objects.update_or_create(
                    patient=patient,
                    course_number=course_number,
                    timing=timing,
                    type='HAM-D',
                    defaults=legacy_defaults,
                )
            else:
                legacy_obj = None

            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                total_17 = new_record.total_score_17
                improvement = new_record.improvement_rate_17
                status = new_record.status_label
                msg = ""
                # Week-3 messages
                if timing == 'week3':
                    if status == '寛解':
                        msg = "寛解と判定されました。漸減プロトコルへの移行を検討してください。"
                    elif status == '反応なし':
                        msg = "反応が見られません。治療の継続または中止を検討してください。"
                    else:
                        msg = "反応が見られます。治療を継続してください。"
                return JsonResponse({
                    'status': 'success',
                    'id': new_record.id,
                    'total_17': total_17,
                    'improvement_rate': improvement,
                    'status_label': status,
                    'message': msg,
                })

            if from_page == 'clinical_path':
                q = {'focus': assessed_date.strftime('%Y-%m-%d')}
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
            return HttpResponse('保存に失敗しました。', status=400)

    existing_scores = {}
    if record and getattr(record, 'scores', None):
        existing_scores = record.scores
    elif legacy and getattr(legacy, 'scores', None):
        existing_scores = legacy.scores

    existing_note = ''
    if record and getattr(record, 'note', None):
        existing_note = record.note
    elif legacy and getattr(legacy, 'note', None):
        existing_note = legacy.note

    if scale.code == 'hamd':
        _items, hamd_items_left, hamd_items_right = _hamd_items()

        # Fetch baseline for improvement calculation (if not baseline itself)
        baseline_score_17 = None
        if timing != 'baseline':
            baseline_obj = Assessment.objects.filter(
                patient=patient,
                course_number=course_number,
                timing='baseline',
                type='HAM-D',
            ).order_by('-date').first()
            if baseline_obj:
                baseline_score_17 = baseline_obj.total_score_17

        return render(request, 'rtms_app/assessment/scales/hamd.html', {
            'patient': patient,
            'dashboard_date': dashboard_date,
            'scale': scale,
            'scale_name': scale.name,
            'scale_code': scale.code,
            'initial_timing': timing,
            'initial_timing_display': timing_display,
            'window_start': window_start,
            'window_end': window_end,
            'default_date': default_date,
            'hamd_items_left': hamd_items_left,
            'hamd_items_right': hamd_items_right,
            'existing_scores': existing_scores,
            'existing_note': existing_note,
            'baseline_score_17': baseline_score_17,
            'can_view_audit': can_view_audit(request.user),
        })

    return HttpResponse(status=404)

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

    timing_order = [
        ('baseline', '治療前'),
        ('week3', '3週間目'),
        ('week4', '4週間目'),
        ('week6', '6週間目'),
    ]
    latest_by_timing = {
        t: assessments.filter(timing=t).order_by('-date').first() for t, _ in timing_order
    }
    baseline_obj = latest_by_timing.get('baseline')
    baseline_17 = getattr(baseline_obj, 'total_score_17', None)

    def severity_label_17_fn(val):
        if val is None:
            return None
        if val <= 7:
            return '正常'
        if val <= 13:
            return '軽症'
        if val <= 18:
            return '中等症'
        if val <= 22:
            return '重症'
        return '最重症'

    trend_cols = []
    for t, label in timing_order:
        a = latest_by_timing.get(t)
        date_str = a.date.strftime('%Y/%-m/%-d') if a and getattr(a, 'date', None) else '-'
        hamd17 = getattr(a, 'total_score_17', None)
        hamd21 = getattr(a, 'total_score_21', None)

        improvement_pct = None
        if t != 'baseline' and a and baseline_17 not in (None, 0) and hamd17 is not None:
            improvement_pct = round((baseline_17 - hamd17) / baseline_17 * 100.0, 1)
        status_lbl = classify_hamd_response(hamd17, improvement_pct) if t != 'baseline' else None

        trend_cols.append({
            'timing': t,
            'label': label,
            'date_str': date_str,
            'hamd17': hamd17,
            'hamd21': hamd21,
            'improvement_pct': improvement_pct,
            'severity_label_17': severity_label_17_fn(hamd17),
            'status_label': status_lbl,
        })

    eval_w3 = next((c for c in trend_cols if c['timing'] == 'week3'), None)
    eval_w4 = next((c for c in trend_cols if c['timing'] == 'week4'), None)
    eval_w6 = next((c for c in trend_cols if c['timing'] == 'week6'), None)
    discharge_sidebar = {
        'treatment_count_total': sessions.count(),
        'last_treatment_date': sessions.last().session_date if sessions.exists() else None,
        'eval_week3': {
            'hamd17': eval_w3.get('hamd17') if eval_w3 else None,
            'hamd21': eval_w3.get('hamd21') if eval_w3 else None,
            'improvement_pct': eval_w3.get('improvement_pct') if eval_w3 else None,
            'status_label': classify_hamd_response(eval_w3.get('hamd17') if eval_w3 else None, eval_w3.get('improvement_pct') if eval_w3 else None),
        },
        'eval_week4': {
            'hamd17': eval_w4.get('hamd17') if eval_w4 else None,
            'hamd21': eval_w4.get('hamd21') if eval_w4 else None,
            'improvement_pct': eval_w4.get('improvement_pct') if eval_w4 else None,
            'status_label': classify_hamd_response(eval_w4.get('hamd17') if eval_w4 else None, eval_w4.get('improvement_pct') if eval_w4 else None),
        },
        'eval_week6': {
            'hamd17': eval_w6.get('hamd17') if eval_w6 else None,
            'hamd21': eval_w6.get('hamd21') if eval_w6 else None,
            'improvement_pct': eval_w6.get('improvement_pct') if eval_w6 else None,
            'status_label': classify_hamd_response(eval_w6.get('hamd17') if eval_w6 else None, eval_w6.get('improvement_pct') if eval_w6 else None),
        },
    }
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
    elif sessions.exists(): end_date_str = sessions.last().session_date.strftime('%Y年%m月%d日')
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
            "formaction": reverse("rtms_app:print:patient_print_bundle", args=[patient.id]),
            "formtarget": "_blank",
            "docs_form_id": "bundlePrintFormDischarge",
        },
    ]

    plan_week_no = None
    if patient.first_treatment_date:
        ref_date = None
        if patient.discharge_date:
            ref_date = patient.discharge_date
        elif sessions.exists():
            ref_date = sessions.last().session_date
        else:
            ref_date = timezone.localdate()
        plan_week_no = get_current_week_number(patient.first_treatment_date, ref_date)

    return render(request, 'rtms_app/patient_summary.html', {
        'patient': patient,
        'summary_text': summary_text,
        'history_list': history_list,
        'today': timezone.now().date(),
        'test_scores': test_scores,
        'trend_cols': trend_cols,
        'discharge_sidebar': discharge_sidebar,
        'total_count': total_count,
        'end_date_str': end_date_str,
        'start_date_str': start_date_str,
        'dashboard_date': dashboard_date,
        'floating_print_options': floating_print_options,
        'can_view_audit': can_view_audit(request.user),
        # Unified plan bar variables
        'treatment_plan_start': patient.first_treatment_date,
        'treatment_plan_end': get_completion_date(patient.first_treatment_date),
        'today_session_no': sessions.count() if sessions.exists() else 0,
        'total_sessions': 30,
        'week_no': plan_week_no,
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
    response = HttpResponse(content_type='text/csv; charset=utf-8-sig'); response['Content-Disposition'] = 'attachment; filename="treatment_data.csv"'; writer = csv.writer(response); writer.writerow(['ID', '氏名', '実施日時', 'MT値', '刺激強度(%MT)', 'パルス数', '実施者', '副作用'])
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

    legacy_map = {"consent": "consent_pdf"}
    raw_docs = [legacy_map.get(doc, doc) for doc in raw_docs]

    DOC_DEFINITIONS = {
        "admission": {"label": "初診時サマリー", "template": "rtms_app/print/admission_summary.html"},
        "suitability": {"label": "rTMS問診票", "template": "rtms_app/print/suitability_questionnaire.html"},
        "consent_pdf": {"label": "説明同意書（PDF）", "pdf_static": "rtms_app/docs/rtms_consent_latest.pdf"},
        "discharge": {"label": "退院時サマリー", "template": "rtms_app/print/discharge_summary.html"},
        "hamd_detail": {"label": "HAMD詳細票", "template": "rtms_app/print/hamd_detail.html"},
        "referral": {"label": "紹介状", "template": "rtms_app/print/referral.html"},
    }
    DOC_ORDER = ["admission", "suitability", "consent_pdf", "discharge", "hamd_detail", "referral"]

    selected_doc_keys = [d for d in DOC_ORDER if d in raw_docs]

    assessments = Assessment.objects.filter(patient=patient).order_by("date")

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

    # HAMD trend columns (baseline, 3週間目, 4週間目, 6週間目)
    timing_order = [("baseline", "治療前"), ("week3", "3週間目"), ("week4", "4週間目"), ("week6", "6週間目")]
    latest_by_timing = {t: assessments.filter(timing=t).order_by("-date").first() for t, _ in timing_order}
    baseline_obj = latest_by_timing.get("baseline")
    baseline_17 = getattr(baseline_obj, "total_score_17", None)
    baseline_21 = getattr(baseline_obj, "total_score_21", None)
    hamd_trend_cols = []
    for t, label in timing_order:
        a = latest_by_timing.get(t)
        date_str = a.date.strftime("%Y/%-m/%-d") if a and getattr(a, "date", None) else "-"
        hamd17 = getattr(a, "total_score_17", None)
        hamd21 = getattr(a, "total_score_21", None)
        improvement_pct_17 = None
        improvement_pct_21 = None
        if t != "baseline" and a:
            if baseline_17 not in (None, 0) and hamd17 is not None:
                improvement_pct_17 = round((baseline_17 - hamd17) / baseline_17 * 100.0, 1)
            if baseline_21 not in (None, 0) and hamd21 is not None:
                improvement_pct_21 = round((baseline_21 - hamd21) / baseline_21 * 100.0, 1)
        hamd_trend_cols.append({
            "timing": t,
            "label": label,
            "date_str": date_str,
            "hamd17": hamd17,
            "hamd21": hamd21,
            "improvement_pct_17": improvement_pct_17,
            "improvement_pct_21": improvement_pct_21,
            "status_label": classify_hamd_response(hamd17, improvement_pct_17) if t != "baseline" else None,
            "status_label_21": None,
        })

    # HAMD detail grid (per-item scores)
    assess_map = {t: latest_by_timing.get(t) for t, _ in timing_order}
    cols = []
    for t, label in timing_order:
        a = assess_map.get(t)
        cols.append({
            "key": t,
            "label": label,
            "date_str": a.date.strftime("%Y/%-m/%-d") if a and getattr(a, "date", None) else "-",
        })
    rows = []
    ITEM_NAMES = [f"項目{i}" for i in range(1, 22)]
    for i in range(1, 22):
        name = ITEM_NAMES[i - 1]
        scores_by_t = {}
        for t, _ in timing_order:
            a = assess_map.get(t)
            val = None
            if a and isinstance(a.scores, dict):
                val = a.scores.get(f"q{i}")
            scores_by_t[t] = val if val is not None else ""
        rows.append({"no": i, "name": name, "scores": scores_by_t})
    totals = {"hamd17": {}, "hamd21": {}, "improvement_pct": {}, "severity": {}, "status_label": {}}
    baseline17 = getattr(assess_map.get("baseline"), "total_score_17", None)
    for t, _ in timing_order:
        a = assess_map.get(t)
        t17 = getattr(a, "total_score_17", None)
        t21 = getattr(a, "total_score_21", None)
        totals["hamd17"][t] = t17
        totals["hamd21"][t] = t21
        totals["severity"][t] = classify_hamd17_severity(t17)
        if t == "baseline" or (baseline17 in (None, 0) or t17 is None):
            totals["improvement_pct"][t] = None
        else:
            totals["improvement_pct"][t] = round((baseline17 - t17) / baseline17 * 100.0, 1)
        totals["status_label"][t] = classify_hamd_response(t17, totals["improvement_pct"].get(t)) if t != "baseline" else None

    context = {
        "patient": patient,
        "docs_to_render": docs_to_render,
        "doc_definitions": DOC_DEFINITIONS,
        "selected_doc_keys": selected_doc_keys,
        "assessments": assessments,
        "test_scores": assessments,
        "hamd_trend_cols": hamd_trend_cols,
        "hamd_detail": {"cols": cols, "rows": rows, "totals": totals},
        "consent_copies": ["患者控え", "病院控え"],
        "end_date_est": end_date_est,
        "today": today,
        "back_url": back_url,
    }

    # 印刷ログ記録
    for doc_key in selected_doc_keys:
        doc_label = DOC_DEFINITIONS.get(doc_key, {}).get("label", doc_key)
        meta = {"docs": selected_doc_keys, "querystring": request.GET.urlencode(), "return_to": return_to}
        log_audit_action(patient, "PRINT", "Document", doc_key, f"{doc_label}印刷", meta)

    return render(request, "rtms_app/print/bundle.html", context)

@login_required
def patient_clinical_path(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date')
    # ★修正: generate_calendar_weeks を使用
    calendar_weeks, assessment_events = generate_calendar_weeks(patient)
    last_assessment = Assessment.objects.filter(patient=patient, timing='week3').order_by('-date').first()
    baseline_assessment = Assessment.objects.filter(patient=patient, timing='baseline').order_by('-date').first()
    week6_assessment = Assessment.objects.filter(patient=patient, timing='week6').order_by('-date').first()
    # Restore print FAB: provide direct href/target for the floating button
    print_href = reverse('rtms_app:print:print_clinical_path', args=[patient.id])
    floating_print_options = [{
        'label': '印刷プレビュー',
        'icon': 'fa-print',
        'value': 'print_path',
        'formaction': print_href,
        'formmethod': 'get',
        'formtarget': '_blank',
        'href': print_href,
        'target': '_blank',
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
    
    dashboard_date = request.GET.get('dashboard_date')
    return render(request, 'rtms_app/audit_logs.html', {
        'patient': patient,
        'logs': logs,
        'dashboard_date': dashboard_date,
    })

@login_required
def latest_consent(request):
    doc = ConsentDocument.objects.order_by("-uploaded_at").first()
    if doc and doc.file:
        return redirect(doc.file.url)
    # アップロードが無い / 初期化で消えた → 静的ファイルへフォールバック
    return redirect(static("rtms_app/docs/consent_default.pdf"))

@login_required
@require_http_methods(["POST"])
def mapping_upsert_from_wizard(request, patient_id):
    """
    ウィザードから MT測定データを保存する エンドポイント
    POST: { course_number, mt_value, a_x, a_y, b_x, b_y, mapping_date, note }
    """
    import json
    from django.http import JsonResponse
    
    patient = get_object_or_404(Patient, pk=patient_id)
    
    try:
        data = json.loads(request.body)
    except:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    
    course_number = data.get('course_number', patient.course_number or 1)
    mt_value = data.get('mt_value')
    a_x = data.get('a_x', '3')
    a_y = data.get('a_y', '1')
    b_x = data.get('b_x', '9')
    b_y = data.get('b_y', '1')
    mapping_date_str = data.get('mapping_date')
    note = data.get('note', '')
    
    # Validate MT value
    try:
        mt_value = int(mt_value)
        if mt_value < 10 or mt_value > 100:
            return JsonResponse({'success': False, 'error': 'MT value out of range (10-100)'}, status=400)
    except:
        return JsonResponse({'success': False, 'error': 'Invalid MT value'}, status=400)
    
    # Parse mapping_date
    try:
        mapping_date = parse_date(mapping_date_str) if mapping_date_str else timezone.now().date()
    except:
        mapping_date = timezone.now().date()
    
    # Upsert MappingSession
    defaults = {
        'resting_mt': mt_value,
        'helmet_position_a_x': int(a_x),
        'helmet_position_a_y': int(a_y),
        'helmet_position_b_x': int(b_x),
        'helmet_position_b_y': int(b_y),
        'notes': note,
    }
    
    mapping, created = MappingSession.objects.update_or_create(
        patient=patient,
        course_number=course_number,
        date=mapping_date,
        defaults=defaults
    )
    
    return JsonResponse({
        'success': True,
        'mapping_id': mapping.id,
        'created': created,
        'mt_value': mapping.resting_mt,
    })


@superuser_required
@require_http_methods(['GET', 'POST'])
def export_research_csv(request):
    """
    Export research CSV in wide format.
    GET: Show checkbox form to select columns/categories
    POST: Generate and download CSV with selected columns
    
    Restricted to superusers only.
    """
    from .services.export_research import ResearchCSVExporter
    
    exporter = ResearchCSVExporter()
    
    if request.method == 'GET':
        # Show category selection form
        categories = exporter.get_category_choices()
        context = {
            'categories': categories,
            'title': '研究用CSVエクスポート',
        }
        return render(request, 'rtms_app/export_research_csv.html', context)
    
    elif request.method == 'POST':
        # Get selected categories from POST
        selected_categories = request.POST.getlist('categories')
        if not selected_categories:
            selected_categories = list(exporter.CATEGORIES.keys())
        
        # Create exporter with selected categories
        exporter = ResearchCSVExporter(selected_categories=selected_categories)
        
        # Fetch all patients (consider pagination for large datasets)
        patients = Patient.objects.all().order_by('card_id', 'course_number')
        
        # Prepare data: list of (patient, related_data) tuples
        patients_data = [(p, None) for p in patients]
        
        # Generate CSV
        csv_content = exporter.generate_csv(patients_data)
        
        # Log action
        log_audit_action(
            None, 'EXPORT', 'ResearchCSV', '',
            f'研究用CSV: {len(selected_categories)}カテゴリ選択',
            {'selected_categories': selected_categories, 'patient_count': len(patients)}
        )
        
        # Return as downloadable file
        response = HttpResponse(csv_content, content_type='text/csv; charset=utf-8-sig')
        response['Content-Disposition'] = f'attachment; filename="research_data_{timezone.now().strftime("%Y%m%d_%H%M%S")}.csv"'
        return response


@login_required
@require_http_methods(['GET', 'POST'])
def admin_backup(request):
    """
    Admin backup management screen.
    GET: Show backup/export options
    POST: Handle dumpdata JSON export
    """
    if not request.user.is_staff:
        return redirect('login')
    
    if request.method == 'GET':
        context = {
            'title': 'バックアップ管理',
        }
        return render(request, 'rtms_app/admin_backup.html', context)
    
    elif request.method == 'POST':
        action = request.POST.get('action')
        
        if action == 'dumpdata':
            # Generate dumpdata JSON
            from django.core.management import call_command
            import json
            
            output = io.StringIO()
            try:
                call_command('dumpdata', 'rtms_app', stdout=output, indent=2)
                json_content = output.getvalue()
                
                # Log action
                log_audit_action(
                    None, 'EXPORT', 'DatabaseDumpData', '',
                    'データベースダンプ（JSON）をエクスポート'
                )
                
                # Return as downloadable file
                response = HttpResponse(json_content, content_type='application/json; charset=utf-8')
                response['Content-Disposition'] = f'attachment; filename="dumpdata_{timezone.now().strftime("%Y%m%d_%H%M%S")}.json"'
                return response
            except Exception as e:
                context = {
                    'title': 'バックアップ管理',
                    'error': f'ダンプデータ生成エラー: {str(e)}',
                }
                return render(request, 'rtms_app/admin_backup.html', context, status=500)
        
        elif action == 'download_db':
            # Download SQLite database file
            db_path = settings.DATABASES['default']['NAME']
            if not os.path.exists(db_path):
                context = {
                    'title': 'バックアップ管理',
                    'error': 'データベースファイルが見つかりません',
                }
                return render(request, 'rtms_app/admin_backup.html', context, status=404)
            
            log_audit_action(
                None, 'EXPORT', 'DatabaseFile', '',
                'SQLiteデータベースファイルをダウンロード'
            )
            
            response = FileResponse(open(db_path, 'rb'))
            response['Content-Disposition'] = f'attachment; filename="db_{timezone.now().strftime("%Y%m%d_%H%M%S")}.sqlite3"'
            response['Content-Type'] = 'application/octet-stream'
            return response
        
        else:
            context = {
                'title': 'バックアップ管理',
                'error': '不正なアクション',
            }
            return render(request, 'rtms_app/admin_backup.html', context, status=400)


