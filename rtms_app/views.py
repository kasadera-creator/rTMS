from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time
from django.utils.safestring import mark_safe
from django.urls import reverse
from django.templatetags.static import static
from datetime import timedelta, date
import datetime
from django.http import HttpResponse, FileResponse, JsonResponse, HttpResponseBadRequest
from django.conf import settings
from django.contrib.auth import logout
from django.db import IntegrityError, transaction
from django.db.models import Q
import os
import csv
import json
import logging
import calendar as pycalendar
from urllib.parse import urlencode

from rtms_app.surveys import INSTRUMENT_ORDER, instrument_label

# Module-level trace - write to stderr so it's always visible
import sys
sys.stderr.write("===== views.py module loaded =====\n")
sys.stderr.flush()

from .models import (
    Patient, TreatmentCourse, TreatmentSession, MappingSession, MappingSchedule, Assessment, AssessmentRecord,
    ScaleDefinition, TimingScaleConfig, AssessmentSchedule, ConsentDocument, AuditLog,
    SideEffectCheck, TreatmentSkip, PatientSurveySession, AdverseEventReport,
    SeriousAdverseEvent,
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
    keys = [question['key'] for question in (questions_past + questions_current)] + ['q_details']
    return questions_past, questions_current, keys

from .services.schedule_tasks import compute_dashboard_tasks
from .services.schedule import (
    MAX_TREATMENT_SESSIONS,
    can_create_treatment_session,
    get_treatment_overflow_info,
    get_treatment_course_end_date,
    get_treatment_session_number_map,
    get_treatment_sessions,
    get_treatment_virtual_number_map,
    shift_future_sessions,
    reschedule_planned_session,
    reschedule_treatment_start_date,
)
from .view_helpers import extract_back_url, get_dashboard_date, build_common_context, get_course_number
from .queries.assessment_queries import (
    get_assessments_ordered,
    get_latest_assessment,
    get_assessment_by_timing_with_fallback,
    save_assessment_record,
    save_assessment_record_legacy,
    save_assessment_hamd,
    save_assessment_hamd_legacy,
    resolve_treatment_course,
)
from .assessment_rules import classify_response_status, compute_improvement_rate
from .services.assessment_scales import (
    SIMPLE_SCALE_CODES, DETAIL_FIELDS, COPM_FIELDS, SIX_MWT_VITAL_FIELDS,
    SIX_MWT_SUBJECTIVE_FIELDS,
    TINKERTOY_FIELDS, build_detail_scores, detail_form_context, parse_simple_score,
    staff_timing_label, summary_for_record,
)
from .services.strict_writes import (
    bulk_create_treatment_sessions_strict,
    bulk_create_treatment_sessions_legacy,
    get_or_create_treatment_session_strict,
    save_mapping_session_legacy,
    save_mapping_session_strict,
    save_treatment_session_legacy,
    save_treatment_session_strict,
    update_or_create_treatment_session_legacy,
    save_mapping_session_legacy,
    save_mapping_session_strict,
    update_or_create_assessment_schedule_legacy,
    update_or_create_assessment_schedule_strict,
    update_or_create_mapping_schedule_legacy,
    update_or_create_mapping_schedule_strict,
    update_or_create_treatment_session_strict,
)
from .services.patient_registration import register_patient_with_initial_course

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
        ws = patient.first_visit_date or (patient.created_at.date() if patient.created_at else timezone.localdate())
        we = patient.first_treatment_date or ws
        # Existing baseline design ends at the first treatment date. If legacy
        # registration data is later than that deadline, use the deadline as
        # the valid start rather than creating an inverted interval.
        if patient.first_treatment_date and ws > we:
            ws = we
        return ws, we

    if timing.startswith('tinkertory_'):
        if not patient.first_treatment_date:
            today = timezone.localdate()
            return today, today
        week_index = int(timing.rsplit('_', 1)[-1])
        start = patient.first_treatment_date + timedelta(days=(week_index - 1) * 7)
        end = start + timedelta(days=6)
        return start, end

    if not patient.first_treatment_date:
        today = timezone.localdate()
        return today, today

    ft = patient.first_treatment_date
    week_number = {'week3': 3, 'week4': 4, 'week6': 6}.get(timing)
    if week_number is None:
        today = timezone.localdate()
        return today, today
    treatment_dates = generate_treatment_dates(ft, total=30, holidays=JP_HOLIDAYS)
    week_dates = treatment_dates[(week_number - 1) * 5:week_number * 5]
    if week_dates:
        return week_dates[0], week_dates[-1]
    # 治療予定が不足する極端なケースでは従来の暦日範囲へフォールバック。
    raw_start = ft + timedelta(days=(week_number - 1) * 7)
    raw_end = raw_start + timedelta(days=6)
    return _first_last_treatment_day_in_range(raw_start, raw_end)

# --- ヘルパー関数 ---

def build_url(name, args=None, query=None):
    resolved_name = name if ':' in name else f'rtms_app:{name}'
    base = reverse(resolved_name, args=args)
    return f'{base}?{urlencode(query, doseq=True)}' if query else base


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
    if not start_date:
        return 0
    # normalize to date objects if datetimes provided
    try:
        s = start_date.date() if hasattr(start_date, 'date') else start_date
    except Exception:
        s = start_date
    try:
        t = target_date.date() if hasattr(target_date, 'date') else target_date
    except Exception:
        t = target_date

    # If target earlier than start, indicate "pre-treatment" with 0
    if t < s:
        return 0

    days_diff = (t - s).days
    return (days_diff // 7) + 1


def get_week3_hamd17_evaluation(patient, course_number, current_week):
    """Build the week-3 HAM-D17 instruction for the treatment-entry screen."""
    evaluation = {
        'available': False,
        'score': None,
        'improvement_pct': None,
        'status': None,
        'instruction': '',
    }
    if current_week < 3:
        return evaluation | {'week3_status': '評価期間前'}

    hamd_scale = ScaleDefinition.objects.filter(code='hamd').first()
    if hamd_scale:
        week3_assessment = get_assessment_by_timing_with_fallback(
            patient, 'week3', hamd_scale, course_number
        )
        baseline_assessment = get_assessment_by_timing_with_fallback(
            patient, 'baseline', hamd_scale, course_number
        )
    else:
        week3_assessment = Assessment.objects.filter(
            patient=patient, course_number=course_number, timing='week3', type='HAM-D'
        ).order_by('-date').first()
        baseline_assessment = Assessment.objects.filter(
            patient=patient, course_number=course_number, timing='baseline', type='HAM-D'
        ).order_by('-date').first()

    if week3_assessment is None:
        return evaluation | {
            'week3_status': 'HAM-Dを実施してください',
            'instruction': 'HAM-Dを実施してください',
        }

    score = week3_assessment.total_score_17
    baseline_score = getattr(baseline_assessment, 'total_score_17', None)
    improvement = compute_improvement_rate(baseline_score, score)
    improvement_pct = round(improvement * 100, 1) if improvement is not None else None
    status = classify_response_status(score, improvement)
    if status == '寛解':
        instruction = '寛解：治療を中止または漸減してください'
        taper_limits = {4: '第4週：最大週3回', 5: '第5週：最大週2回', 6: '第6週：最大週1回'}
        if current_week in taper_limits:
            instruction = f'{instruction}（{taper_limits[current_week]}）'
    elif improvement is None:
        instruction = '治療開始前HAM-D17が未入力または0点のため、改善率を判定できません'
    elif improvement < 0.20:
        instruction = '改善不十分：治療を中止してください'
    else:
        instruction = '治療継続'

    return evaluation | {
        'available': True,
        'score': score,
        'improvement_pct': improvement_pct,
        'status': status,
        'instruction': instruction,
        'week3_status': '第3週評価：実施済み',
    }

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


def get_hamd_effective_deadline(base_date, today=None):
    """
    HAM-D の「未実施なら翌治療日へ1日ずつ順延」ルールを適用した実効予定日を返す。
    today が base_date 以前ならそのまま base_date。today が過ぎていれば、
    base_date 翌日から today までの間で最後に到来した治療日まで順延する
    （today 自身が治療日ならその日、休日等ならその直前の治療日）。
    """
    if today is None:
        today = timezone.localdate()
    if today <= base_date:
        return base_date
    effective = base_date
    cur = base_date + timedelta(days=1)
    while cur <= today:
        if is_treatment_day(cur):
            effective = cur
        cur += timedelta(days=1)
    return effective


def get_hamd_baseline_default_date(patient):
    """HAM-D 治療前評価のデフォルト予定日（初診日、未設定時は登録日）"""
    first_visit = patient.first_visit_date or (patient.created_at.date() if patient.created_at else timezone.localdate())
    if patient.first_treatment_date and first_visit > patient.first_treatment_date:
        return patient.first_treatment_date
    return first_visit


def get_scale_baseline_default_date(patient):
    """HAM-D以外の尺度の治療前評価デフォルト予定日（入院日）"""
    return patient.admission_date or get_hamd_baseline_default_date(patient)


# HAM-D以外の尺度は「治療前尺度評価」「治療後尺度評価」としてまとめて1件で表示・移動する際の scale_code マーカー
OTHER_SCALES_SCHEDULE_CODE = '__other_scales__'


def get_assessment_schedule_default_date(patient, scale, timing, treatment_end_est=None):
    """
    scale/timing に対応するデフォルト予定日（AssessmentSchedule に上書きが無い場合の値）。
    """
    is_hamd = scale.code == 'hamd'
    if timing == 'baseline':
        return get_hamd_baseline_default_date(patient) if is_hamd else get_scale_baseline_default_date(patient)
    if is_hamd and timing in ('week3', 'week6'):
        ws, _ = get_assessment_window(patient, timing)
        return ws
    if is_hamd and timing == 'week4':
        # 4週経過後HAM-D評価は「4週目の最終セッション(予定)日」を基準日とする（週3/週6とは異なる）
        _, we = get_assessment_window(patient, timing)
        return we
    if timing == 'post':
        return treatment_end_est
    return None

# ★修正: カレンダーデータ生成ロジック (週単位のリストを返す)
def generate_calendar_weeks(patient, treatment_course=None, course_number=None):
    if treatment_course is None:
        course_number = course_number or patient.course_number or 1
        treatment_course = TreatmentCourse.objects.filter(
            patient=patient, course_number=course_number,
        ).first()
    if treatment_course is not None and treatment_course.patient_id != patient.id:
        raise ValueError("TreatmentCourse belongs to a different patient")
    course_number = (
        treatment_course.course_number
        if treatment_course is not None
        else course_number or patient.course_number or 1
    )
    course_admission_date = getattr(treatment_course, 'admission_date', None) or patient.admission_date
    course_first_treatment_date = getattr(treatment_course, 'first_treatment_date', None) or patient.first_treatment_date
    course_discharge_date = getattr(treatment_course, 'discharge_date', None) or patient.discharge_date
    course_mapping_date = getattr(treatment_course, 'mapping_date', None) or patient.mapping_date
    # 基準となる開始日
    base_start = course_admission_date or course_first_treatment_date or timezone.now().date()
    treatment_start = course_first_treatment_date
    treatment_end_est = None
    if treatment_start:
        tdates_for_end = generate_treatment_dates(treatment_start, total=30, holidays=JP_HOLIDAYS)
        if tdates_for_end:
            treatment_end_est = tdates_for_end[-1]

    base_end = course_discharge_date
    if not base_end:
        if treatment_end_est:
            base_end = treatment_end_est  # 30回目当日まで
        else:
            base_end = base_start + timedelta(days=30)

    # 開始日が月曜になるように調整
    start_date = base_start - timedelta(days=base_start.weekday())

    # 終了日はその日まで（週末への拡張はしない）。手動移動された
    # TreatmentSession が30回目のcanonical日付を越えていても、保存済みの
    # 実予定を画面から隠さない。
    all_treatment_sessions = get_treatment_sessions(patient, course_number=course_number)
    actual_treatment_sessions = all_treatment_sessions[:MAX_TREATMENT_SESSIONS]
    actual_treatment_dates = [s.session_date for s in actual_treatment_sessions]
    end_date = max([base_end, *actual_treatment_dates]) if actual_treatment_dates else base_end

    calendar_weeks = []
    current_week = []
    current = start_date

    treatments_done = {t.session_date: t for t in all_treatment_sessions}
    assessment_events = []  # 評価イベントを別途収集

    # Canonical planned treatment and mapping dates (no drift, closures honored)
    treat_dates = []
    # MT測定：週番号 -> 予定日（MappingSchedule のドラッグ調整があれば優先。無ければ計算式の値）
    scheduled_mapping_by_date = {}
    mapping_scope = {'treatment_course': treatment_course} if treatment_course else {
        'patient': patient, 'course_number': course_number,
    }
    mapping_overrides = {
        ms.week_number: ms.planned_date
        for ms in MappingSchedule.objects.filter(**mapping_scope)
    }
    if treatment_start:
        treat_dates = generate_treatment_dates(treatment_start, total=30, holidays=JP_HOLIDAYS)
        # Filter out cancelled sessions: remove dates where there's a cancel record or discharge_date is before or on that date
        if course_discharge_date:
            # If patient has a discharge_date set, filter out all treat_dates on or after discharge_date
            treat_dates = [d for d in treat_dates if d < course_discharge_date]

        # Once all 30 regular TreatmentSession rows exist, their chronological
        # last date is the authoritative course end.  Legacy overflow rows and
        # canonical dates must not extend the MT display range.
        treatment_end_est = get_treatment_course_end_date(
            patient, treat_dates, course_number=course_number
        )

        # Use mapping base as patient.mapping_date if set, else first_treatment_date
        mapping_base = course_mapping_date or treatment_start
        if mapping_base:
            mapping_list = generate_mapping_dates(mapping_base, weeks=8, holidays=JP_HOLIDAYS)
            for m in mapping_list:
                wk = m['week_no']
                # A planned MT slot is shown only while its corresponding
                # treatment-course range exists.  generate_mapping_dates()
                # intentionally supports eight weeks, but a 30-session
                # course normally has six treatment weeks.
                if treatment_end_est and m['actual'] > treatment_end_est:
                    continue
                if wk == 1 and course_mapping_date and wk not in mapping_overrides:
                    d = course_mapping_date  # 明示的な初回MT測定日を優先（既存挙動を維持）
                else:
                    d = mapping_overrides.get(wk, m['actual'])
                # Apply the same limit after an explicit MT date override;
                # otherwise a stale/manual override could reintroduce an MT
                # event after the regular 30th treatment.
                if treatment_end_est and d > treatment_end_est:
                    continue
                if course_discharge_date and d >= course_discharge_date:
                    continue
                scheduled_mapping_by_date[d] = wk

    actual_session_by_date = {}
    for ts_row in actual_treatment_sessions:
        if ts_row.status not in {'planned', 'done'}:
            continue
        actual_session_by_date[ts_row.session_date] = ts_row

    # TreatmentSession has no persisted treatment-number field.  Once a real
    # session exists, its chronological position is the number shown to the
    # user; canonical dates only project the remaining not-yet-materialized
    # schedule.  This prevents a moved session on a former canonical date from
    # being relabeled using that date's old canonical index.
    treatment_number_by_id = get_treatment_session_number_map(
        patient, course_number=course_number
    )
    virtual_treatment_number_by_date = get_treatment_virtual_number_map(
        patient, treat_dates, course_number=course_number
    )

    # 実績のあるMT測定（週番号付き）を日付でも参照できるようにする
    actual_mapping_by_date = {}
    for ms_row in MappingSession.objects.filter(**mapping_scope):
        actual_mapping_by_date[ms_row.date] = ms_row

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

        if current == course_admission_date:
            day_info['events'].append({'type': 'admission', 'label': '入院', 'url': build_url('admission_procedure', [patient.id]), 'draggable': True})

        # 2. MT測定（実績があれば実績、なければ週次予定を表示。週ごとに個別ドラッグ調整可・他週への連動なし）
        mapping_actual = actual_mapping_by_date.get(current)
        mapping_week = mapping_actual.week_number if mapping_actual is not None else scheduled_mapping_by_date.get(current)
        if mapping_week is not None:
            is_mapping_done = mapping_actual is not None
            day_info['events'].append({
                'type': 'mapping',
                'label': 'MT測定' + (' (済)' if is_mapping_done else ''),
                'url': build_url("mapping_add", args=[patient.id], query={"date": current.strftime("%Y-%m-%d"), "course_number": course_number}),
                'draggable': True,
                'status': 'done' if is_mapping_done else 'planned',
                'week_number': mapping_week,
                'session_id': mapping_actual.id if mapping_actual is not None else None,
            })

        # 3. 治療予定・実績（実セッションを優先し、残りだけcanonical予定を表示）
        ts = actual_session_by_date.get(current)
        session_no = treatment_number_by_id.get(ts.id) if ts is not None else virtual_treatment_number_by_date.get(current)
        show_treatment_slot = session_no is not None
        if show_treatment_slot:
            week_no = get_current_week_number(treatment_start, current)

            is_done = ts.status == 'done' if ts is not None else current in treatments_done
            status_label = " (済)" if is_done else ""
            label = format_rtms_label(session_no, week_no) if isinstance(session_no, int) and isinstance(week_no, int) else f"治療 ({session_no}/{week_no})"
            day_info['events'].append({
                'type': 'treatment',
                'label': label + status_label,
                'url': build_url('treatment_add', [patient.id], {'date': current, 'course_number': course_number}),
                'draggable': True,
                'status': 'done' if is_done else 'planned',
                'session_id': ts.id if ts is not None else None,
                'first_treatment': session_no == 1,
            })

        # 5. 退院
        if current == course_discharge_date:
            day_info['events'].append({'type': 'discharge', 'label': '退院準備', 'url': build_url('patient_home', [patient.id]), 'draggable': True})

        elif not course_discharge_date and treatment_start:
            # Show discharge prep on the 30th treatment date (not next day)
            if treatment_end_est and current == treatment_end_est:
                day_info['events'].append({'type': 'discharge', 'label': '退院準備', 'url': build_url('patient_home', [patient.id]), 'draggable': True})

        current_week.append(day_info)

        if current.weekday() == 6:
            calendar_weeks.append(current_week)
            current_week = []

        current += timedelta(days=1)

    if current_week: calendar_weeks.append(current_week)

    # 評価イベントを予定日に追加（AssessmentSchedule の上書きがあれば優先。HAM-Dのみ未実施なら自動順延）
    schedule_query = {'treatment_course': treatment_course} if treatment_course else {
        'patient': patient, 'course_number': course_number,
    }
    schedule_overrides = {
        (row.scale_id, row.timing): row.planned_date
        for row in AssessmentSchedule.objects.filter(**schedule_query)
    }
    configured_scales_cal = list(ScaleDefinition.objects.filter(is_active=True).order_by('code'))
    hamd_scale = next((s for s in configured_scales_cal if s.code == 'hamd'), None)
    other_scales_cal = [s for s in configured_scales_cal if s.code != 'hamd']
    hamd_labels = {
        'baseline': '治療前HAM-D評価',
        'week3': '第3週HAM-D評価',
        'week4': '4週経過後HAM-D評価',
        'week6': '第6週HAM-D評価',
    }
    other_scales_labels = {'baseline': '治療前尺度評価', 'post': '治療後尺度評価'}

    def place_assessment_event(timing, label, base_date, is_done, allow_auto_postpone, url_builder, scale_code, deadline=None):
        if base_date is None:
            return
        if not is_done and deadline is not None and timezone.localdate() > deadline:
            return
        effective_date = base_date if is_done else (
            get_hamd_effective_deadline(base_date) if allow_auto_postpone else base_date
        )
        if deadline is not None and effective_date > deadline:
            effective_date = deadline
        if not (start_date <= effective_date <= end_date):
            return
        for week in calendar_weeks:
            for day in week:
                if day['date'] == effective_date:
                    display_label = label + (' (済)' if is_done else '')
                    event = {
                        'type': 'assessment',
                        'label': display_label,
                        'url': url_builder(effective_date),
                        'date': effective_date,
                        'timing': timing,
                        'scale_code': scale_code,
                        'draggable': not is_done,
                        'status': 'done' if is_done else 'planned',
                        'window_end': effective_date,
                    }
                    day['events'].append(event)
                    assessment_events.append(event)
                    return

    assessment_scope = {'treatment_course': treatment_course} if treatment_course else {
        'patient': patient, 'course_number': course_number,
    }
    if hamd_scale:
        hamd_timings = ['baseline', 'week3', 'week6']
        if patient.is_all_case_survey:
            hamd_timings.insert(2, 'week4')
        for timing in hamd_timings:
            base_date = schedule_overrides.get((hamd_scale.id, timing)) or get_assessment_schedule_default_date(patient, hamd_scale, timing, treatment_end_est)
            _, window_end = get_assessment_window(patient, timing)
            deadline = window_end + timedelta(days=7) if timing == 'week4' else window_end
            is_done = (
                Assessment.objects.filter(**assessment_scope, timing=timing, type='HAM-D').exists()
                or AssessmentRecord.objects.filter(**assessment_scope, timing=timing, scale=hamd_scale).exists()
            )
            place_assessment_event(
                timing, hamd_labels[timing], base_date, is_done, allow_auto_postpone=True,
                url_builder=lambda d, t=timing: build_url('assessment_scale', [patient.id, t, hamd_scale.code], query={'from': 'clinical_path', 'date': d.strftime('%Y-%m-%d'), 'course_number': course_number}),
                scale_code=hamd_scale.code,
                deadline=deadline,
            )

    # HAM-D以外の尺度は一度にまとめて実施するため、baseline/post それぞれ1件の
    # 「尺度評価」イベントに集約する。クリック先は尺度選択HUB。ドラッグすると
    # 対象タイミングの全尺度の予定日が連動して移動する。
    if other_scales_cal:
        for timing, label in other_scales_labels.items():
            override_date = None
            for scale in other_scales_cal:
                v = schedule_overrides.get((scale.id, timing))
                if v is not None:
                    override_date = v
                    break
            base_date = override_date or get_assessment_schedule_default_date(patient, other_scales_cal[0], timing, treatment_end_est)
            is_done = all(
                AssessmentRecord.objects.filter(**assessment_scope, timing=timing, scale=scale).exists()
                for scale in other_scales_cal
            )
            place_assessment_event(
                timing, label, base_date, is_done, allow_auto_postpone=False,
                url_builder=lambda d, t=timing: build_url('assessment_add', [patient.id, t], query={'from': 'clinical_path', 'date': d.strftime('%Y-%m-%d'), 'course_number': course_number}),
                scale_code=OTHER_SCALES_SCHEDULE_CODE,
            )

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
    # If no date provided, default to today instead of redirecting.
    if 'date' not in request.GET:
            target_date = timezone.localdate()
    else:
        try:
            target_date = parse_date(request.GET.get('date'))
        except:
            target_date = jst_now.date()
        if not target_date:
            target_date = jst_now.date()
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    target_date_display = f"{target_date.year}年{target_date.month}月{target_date.day}日 ({weekdays[target_date.weekday()]})"
    prev_day = target_date - timedelta(days=1); next_day = target_date + timedelta(days=1)

    task_first_visit = [{'obj': p, 'status': "診察済", 'todo': "初診"} for p in Patient.objects.filter(created_at__date=target_date)]
    task_admission = []; task_mapping = []; task_treatment = []; task_assessment = []; task_discharge = []

    for p in Patient.objects.filter(admission_date=target_date):
        status = "手続済" if p.is_admission_procedure_done else "要手続"; color = "success" if p.is_admission_procedure_done else "warning"
        task_admission.append({'obj': p, 'status': status, 'color': color, 'todo': "入院手続き"})
    for p in Patient.objects.filter(mapping_date=target_date):
        course = TreatmentCourse.objects.filter(
            patient=p, course_number=p.course_number or 1,
        ).first()
        mapping_scope = {'treatment_course': course} if course else {
            'patient': p, 'course_number': p.course_number or 1,
        }
        is_done = MappingSession.objects.filter(**mapping_scope, date=target_date).exists()
        task_mapping.append({'obj': p, 'status': "実施済" if is_done else "実施未", 'color': "success" if is_done else "danger", 'todo': "MT測定"})

    pre_candidates = Patient.objects.filter(admission_date__lte=target_date).filter(Q(first_treatment_date__isnull=True) | Q(first_treatment_date__gte=target_date))
    for p in pre_candidates:
        current_course = TreatmentCourse.objects.filter(
            patient=p, course_number=p.course_number or 1,
        ).first()
        assessment_scope = {'treatment_course': current_course} if current_course else {
            'patient': p, 'course_number': p.course_number or 1,
        }
        ws, we = get_assessment_window(p, 'baseline')
        if ws <= target_date <= we:
            done = Assessment.objects.filter(**assessment_scope, timing='baseline').exists()
            if not done: task_assessment.append({'obj': p, 'status': "実施未", 'color': "danger", 'timing_code': 'baseline', 'todo': f"治療前評価 ({we.strftime('%m/%d')})"})
            elif Assessment.objects.filter(**assessment_scope, timing='baseline', date=target_date).exists(): task_assessment.append({'obj': p, 'status': "実施済", 'color': "success", 'timing_code': 'baseline', 'todo': "治療前評価 (完了)"})

    active_candidates = Patient.objects.filter(first_treatment_date__lte=target_date).order_by('card_id')
    for p in active_candidates:
        current_course = TreatmentCourse.objects.filter(
            patient=p, course_number=p.course_number or 1,
        ).first()
        activity_scope = {'treatment_course': current_course} if current_course else {
            'patient': p, 'course_number': p.course_number or 1,
        }
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
            today_session = TreatmentSession.objects.filter(**activity_scope, date__date=target_date).first(); is_done = today_session is not None
            todo_label = format_rtms_label(n, week)
            task_treatment.append({'obj': p, 'note': '', 'status': "実施済" if is_done else "実施未", 'color': "success" if is_done else "danger", 'session_num': n, 'todo': todo_label})

        # Use get_assessment_window() for week3/week4/week6 to match clinical path windows
        for timing_code, label_name in [('week3', '第3週目評価'), ('week4', '4週経過後HAM-D評価'), ('week6', '第6週目評価')]:
            ws, we = get_assessment_window(p, timing_code)
            if ws and we and target_date == we:
                assessment = Assessment.objects.filter(**activity_scope, timing=timing_code, date__range=[ws, we]).first()
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

    # サービス化したスケジュールタスクをダッシュボードに反映
    # compute_dashboard_tasks は planned_date <= today の未実施タスクを返す
    for p in Patient.objects.all():
        try:
            current_course = TreatmentCourse.objects.filter(
                patient=p, course_number=p.course_number or 1,
            ).first()
            svc_tasks = compute_dashboard_tasks(
                p, today=target_date, holidays=JP_HOLIDAYS,
                treatment_course=current_course,
            )
        except Exception:
            svc_tasks = []
        for tt in svc_tasks:
            key = tt.get('key', '')
            label = tt.get('label') or '未実施タスク'
            perf = tt.get('performed_date')
            if key == 'mapping':
                task_mapping.append({'obj': p, 'status': "実施済" if perf else "実施未", 'color': "success" if perf else "danger", 'todo': label})
            elif key.startswith('assessment'):
                timing = key.replace('assessment_', '')
                task_assessment.append({'obj': p, 'status': "実施済" if perf else "実施未", 'color': "success" if perf else "danger", 'timing_code': timing, 'todo': label})

    dashboard_tasks = [{'list': task_first_visit, 'title': "① 初診", 'color_class': "bg-g-first-visit", 'icon': "fa-user-plus"}, {'list': task_admission, 'title': "② 入院", 'color_class': "bg-g-admission", 'icon': "fa-procedures"}, {'list': task_mapping, 'title': "③ MT測定", 'color_class': "bg-g-mapping", 'icon': "fa-crosshairs"}, {'list': task_treatment, 'title': "④ 治療実施", 'color_class': "bg-g-treatment", 'icon': "fa-bolt"}, {'list': task_assessment, 'title': "⑤ 尺度評価", 'color_class': "bg-g-assessment", 'icon': "fa-clipboard-check"}, {'list': task_discharge, 'title': "⑥ 退院準備", 'color_class': "bg-g-discharge", 'icon': "fa-file-export"}]
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
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date')
    course_number = int(request.GET.get('course_number') or request.POST.get('course_number') or patient.course_number or 1)
    treatment_course = resolve_treatment_course(patient, course_number=course_number)
    mapping_scope = {'treatment_course': treatment_course} if treatment_course else {
        'patient': patient, 'course_number': course_number,
    }
    history = MappingSession.objects.filter(**mapping_scope).order_by('date')

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

    existing_session = MappingSession.objects.filter(
        **mapping_scope, date=initial_date,
    ).first()

    if request.method == 'POST':
        form = MappingForm(request.POST)
        if form.is_valid():
            inst = form.save(commit=False)
            if treatment_course is not None:
                save_mapping_session_strict(
                    inst, patient, treatment_course, course_number=course_number,
                )
            else:
                save_mapping_session_legacy(inst, patient, course_number)
            key_date = inst.date
            action = request.POST.get('action', '')
            if action == 'to_treatment':
                query_params = {'date': key_date.strftime('%Y-%m-%d')}
                if dashboard_date:
                    query_params['dashboard_date'] = dashboard_date
                return redirect(build_url('treatment_add', args=[patient.id], query=query_params))
            if dashboard_date:
                return redirect(f"/app/dashboard/?date={dashboard_date}")
            return redirect('rtms_app:dashboard')
    else:
        form = MappingForm(instance=existing_session) if existing_session else MappingForm(initial={
            'date': initial_date,
            'week_number': week_no_default,
        })

    return render(request, 'rtms_app/mapping_add.html', {
        'patient': patient,
        'form': form,
        'history': history,
        'dashboard_date': dashboard_date,
        'week_no_default': week_no_default,
        'can_view_audit': can_view_audit(request.user)
    })

@login_required
def patient_first_visit(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date')
    treatment_course = resolve_treatment_course(
        patient,
        course_number=request.GET.get('course_number') or request.POST.get('course_number'),
    )
    course_number = treatment_course.course_number if treatment_course else patient.course_number or 1

    all_patients = Patient.objects.all(); referral_map = {}; referral_sources_set = set()
    for p in all_patients:
        if p.referral_source: referral_sources_set.add(p.referral_source);
        if p.referral_doctor:
            if p.referral_source not in referral_map: referral_map[p.referral_source] = set()
            referral_map[p.referral_source].add(p.referral_doctor)
    referral_map_json = {k: sorted(list(v)) for k, v in referral_map.items()}; referral_options = sorted(list(referral_sources_set))
    course_first_treatment_date = (
        treatment_course.first_treatment_date
        if treatment_course and treatment_course.first_treatment_date
        else patient.first_treatment_date
    )
    end_date_est = get_completion_date(course_first_treatment_date)
    assessment_scope = {'treatment_course': treatment_course} if treatment_course else {
        'patient': patient, 'course_number': course_number,
    }
    baseline_assessment = Assessment.objects.filter(**assessment_scope, timing='baseline').first()
    questionnaire = patient.questionnaire_data or {}
    questionnaire_done = bool(questionnaire)

    if request.method == 'POST':
        # card_id/name/birth_date/gender are registered at patient_add; keep stable here.
        # If the template omits these fields (read-only), ensure POST still validates.
        post = request.POST.copy()
        for f in ('card_id', 'name', 'birth_date', 'gender'):
            if not (post.get(f) or '').strip():
                v = getattr(patient, f)
                if hasattr(v, 'isoformat'):
                    v = v.isoformat()
                post[f] = str(v)

        old_first_treatment_date = patient.first_treatment_date
        old_admission_date = patient.admission_date
        form = PatientFirstVisitForm(post, instance=patient)
        if form.is_valid():
            p = form.save(commit=False)
            treatment_start_changed = p.first_treatment_date != old_first_treatment_date
            admission_date_changed = p.admission_date != old_admission_date
            diag_list = request.POST.getlist('diag_list')
            history_codes = [code for code in request.POST.getlist('psychiatric_history') if code != 'F32']
            history_labels = dict(PatientFirstVisitForm.PSY_HISTORY_CHOICES)
            diagnosis_items = diag_list + [history_labels[code] for code in history_codes if code in history_labels]
            diag_other = (request.POST.get('diag_other') or request.POST.get('psychiatric_history_other_text') or '').strip()
            if diagnosis_items or diag_other:
                full_diagnosis = ", ".join(dict.fromkeys(diagnosis_items))
                if diag_other:
                    full_diagnosis += f", その他({diag_other})"
                p.diagnosis = full_diagnosis

            if treatment_start_changed:
                try:
                    reschedule_treatment_start_date(
                        patient,
                        p.first_treatment_date,
                        course_number=patient.course_number or 1,
                        holidays=JP_HOLIDAYS,
                        allow_exceptional_day=bool(
                            p.first_treatment_date and not is_treatment_day(p.first_treatment_date)
                        ),
                    )
                    # Keep the form instance from restoring the old MT base.
                    p.mapping_date = p.first_treatment_date
                except ValueError as exc:
                    form.add_error('first_treatment_date', str(exc))
                    p = None

            if p is not None:
                if treatment_course is not None and treatment_course.course_number != 1:
                    p.first_treatment_date = old_first_treatment_date
                    if admission_date_changed:
                        treatment_course.admission_date = p.admission_date
                    course_update_fields = []
                    if admission_date_changed:
                        course_update_fields.append('admission_date')
                    if course_update_fields:
                        treatment_course.save(update_fields=course_update_fields)
                    p.admission_date = old_admission_date
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
        'referral_options': referral_options,
        'referral_map_json': json.dumps(referral_map_json, ensure_ascii=False),
        'end_date_est': end_date_est,
        'dashboard_date': dashboard_date,
        'baseline_assessment': baseline_assessment,
        'questionnaire_done': questionnaire_done,
        'floating_print_options': floating_print_options,
        'can_view_audit': can_view_audit(request.user),
        'can_edit_basic': can_view_audit(request.user),
    })

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
    modal_mode = request.GET.get('modal') == '1'

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

        # Ajax / modal 保存時は JSON を返す
        if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('modal') == '1':
            return JsonResponse({
                'status': 'success',
                'redirect_url': f"{reverse('rtms_app:patient_first_visit', args=[patient.id])}?dashboard_date={dashboard_date}" if dashboard_date else reverse('rtms_app:patient_first_visit', args=[patient.id]),
            })

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
        'modal_mode': modal_mode,
    })

@login_required
def treatment_add(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id); dashboard_date = request.GET.get('dashboard_date')
    target_date_str = request.GET.get('date'); now = timezone.localtime(timezone.now())
    if target_date_str:
        t = parse_date(target_date_str)
        initial_date = t or now.date()
    else: initial_date = now.date()
    requested_course_number = request.GET.get('course_number') or request.POST.get('course_number')
    if requested_course_number:
        try:
            course_number = int(requested_course_number)
        except (TypeError, ValueError):
            return HttpResponseBadRequest('クール番号が不正です')
    else:
        course_number = patient.course_number or 1
    treatment_course = resolve_treatment_course(patient, course_number=course_number)
    if requested_course_number and treatment_course is None:
        return HttpResponseBadRequest('対象の治療クールが見つかりません')
    session_scope = {'treatment_course': treatment_course} if treatment_course else {
        'patient': patient, 'course_number': course_number,
    }
    mapping_scope = {'treatment_course': treatment_course} if treatment_course else {
        'patient': patient, 'course_number': course_number,
    }

    if request.method == 'POST' and request.POST.get('action') == 'save_sae_report':
        session_date = parse_date(request.POST.get('treatment_date') or '') or initial_date
        session = TreatmentSession.objects.filter(
            **session_scope, session_date=session_date, slot=''
        ).first()
        if session is None:
            return JsonResponse({'error': '対象の治療セッションが見つかりません。'}, status=404)

        def report_int(name):
            try:
                return int(request.POST.get(name)) if request.POST.get(name) else None
            except (TypeError, ValueError):
                return None

        def report_date(name):
            value = request.POST.get(name, '')
            return parse_date(value) if value else None

        diagnosis = request.POST.get('diagnosis', 'うつ病エピソード')
        diagnosis_category = {
            'うつ病エピソード': 'depressive_episode',
            '反復性うつ病性障害': 'recurrent_depressive_disorder',
        }.get(diagnosis, 'other')
        existing_sae = SeriousAdverseEvent.objects.filter(
            patient=patient, course_number=course_number, session=session
        ).first()
        report, _ = AdverseEventReport.objects.update_or_create(
            session=session,
            defaults={
                'event_types': existing_sae.event_types if existing_sae else [],
                'adverse_event_name': (request.POST.get('event_name') or '').strip(),
                'onset_date': report_date('onset_date'),
                'age': report_int('age'),
                'sex': (request.POST.get('gender') or '').strip(),
                'initials': (request.POST.get('initials') or '').strip(),
                'diagnosis_category': diagnosis_category,
                'diagnosis_other_text': (request.POST.get('diagnosis_other') or '').strip(),
                'concomitant_meds_text': request.POST.get('concomitant_meds', ''),
                'substance_intake_text': request.POST.get('substance_use', ''),
                'seizure_history_flag': request.POST.get('seizure_history') == 'あり',
                'seizure_history_date_text': request.POST.get('seizure_history_detail', ''),
                'rmt_value': report_int('rmt'),
                'intensity_value': report_int('intensity'),
                'stimulation_site_category': 'other' if request.POST.get('site') == 'その他' else 'left_dlpfc',
                'stimulation_site_other_text': request.POST.get('site', '') if request.POST.get('site') == 'その他' else '',
                'treatment_course_number': report_int('treatment_number'),
                'outcome_flags': [request.POST.get('outcome')] if request.POST.get('outcome') else [],
                'outcome_date': report_date('outcome_date'),
                'special_notes': request.POST.get('notes', ''),
                'physician_comment': request.POST.get('doctor_comment', ''),
                'prefilled_snapshot': {'source': 'treatment_add', 'session_date': session_date.isoformat()},
            },
        )
        return JsonResponse({'status': 'success', 'report_id': report.id})

    if request.method == 'POST' and request.POST.get('action') == 'clear_side_effects':
        session_date = parse_date(request.POST.get('treatment_date') or '') or initial_date
        session = TreatmentSession.objects.filter(
            patient=patient, course_number=course_number, session_date=session_date, slot=''
        ).first()
        if session is None:
            return JsonResponse({'error': '対象の治療セッションが見つかりません。'}, status=404)
        SideEffectCheck.objects.update_or_create(
            session=session,
            defaults={'rows': [], 'memo': '副作用なし', 'physician_signature': ''},
        )
        SeriousAdverseEvent.objects.filter(session=session).delete()
        AdverseEventReport.objects.filter(session=session).delete()
        return JsonResponse({'status': 'success', 'side_effect_status': '副作用なし'})
    # Session number is derived from materialized TreatmentSession order.
    session_num = None

    # Calculate week_no with day-based rollover anchored to first treatment date
    week_num = 1
    current_week_mapping = None
    mapping_alert = None

    # 上部患者バー右側：モード切替（治療画面専用）
    mode_switch_html = mark_safe(
        """
        <div class=\"btn-group\" role=\"group\" aria-label=\"mode-switch\">
            <input type=\"radio\" class=\"btn-check\" name=\"modeSwitch\" id=\"treatModeRecord\" autocomplete=\"off\" checked>
            <label class=\"btn btn-success btn-sm\" for=\"treatModeRecord\">
                <i class=\"fas fa-pen me-1\"></i>治療内容記入モード
            </label>

            <input type=\"radio\" class=\"btn-check\" name=\"modeSwitch\" id=\"treatModeWizard\" autocomplete=\"off\">
            <label class=\"btn btn-outline-success btn-sm\" for=\"treatModeWizard\">
                <i class=\"fas fa-route me-1\"></i>手順解説モード
            </label>
        </div>
        """
    )
    if patient.first_treatment_date:
        tdates = generate_treatment_dates(patient.first_treatment_date, total=30, holidays=JP_HOLIDAYS)
        materialized_sessions = get_treatment_sessions(patient, course_number)
        number_map = get_treatment_session_number_map(patient, course_number)
        virtual_number_map = get_treatment_virtual_number_map(patient, tdates, course_number)
        current_session = next(
            (item for item in materialized_sessions if item.session_date == initial_date),
            None,
        )
        if current_session is not None:
            session_num = number_map.get(current_session.id)
        else:
            session_num = virtual_number_map.get(initial_date)
        week_num = get_current_week_number(patient.first_treatment_date, initial_date)

    # Fetch current week mapping: same date first, then same week
    same_date_mapping = MappingSession.objects.filter(**mapping_scope, date=initial_date).first()
    if same_date_mapping:
        current_week_mapping = same_date_mapping
    else:
        # Try to get mapping for current week_number
        current_week_mapping = MappingSession.objects.filter(
            **mapping_scope, week_number=week_num,
        ).order_by('-date').first()

    end_date_est = get_completion_date(patient.first_treatment_date)
    hamd_eval = get_week3_hamd17_evaluation(patient, course_number, week_num)
    alert_msg = ""; instruction_msg = hamd_eval['instruction']; judgment_info = hamd_eval['status']
    is_remission = hamd_eval['status'] == '寛解'
    week3_status = hamd_eval['week3_status']
    week3_assessment_url = build_url('assessment_add', args=[patient.id, 'week3'], query={'date': initial_date.isoformat(), 'from': 'treatment'})
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
        if not form.is_valid():
            logger.warning('Treatment form validation failed: %s', form.errors.as_json())
        else:
            cleaned = form.cleaned_data
            d = cleaned['treatment_date']; t = cleaned['treatment_time']
            dt = datetime.datetime.combine(d, t); aware_dt = timezone.make_aware(dt)
            session_date = d
            slot = ''
            # Check safety conditions for warning
            safety_sleep = cleaned.get('safety_sleep', True)
            safety_alcohol = cleaned.get('safety_alcohol', True)
            safety_meds = cleaned.get('safety_meds', True)

            existing_treatment = TreatmentSession.objects.filter(
                **session_scope,
                session_date=session_date,
                slot=slot,
            ).first()
            if existing_treatment is None and not can_create_treatment_session(patient, course_number):
                message = 'この患者・コースの治療予定は30回に達しているため、新規登録できません。'
                if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.headers.get('Accept', '').startswith('application/json'):
                    return JsonResponse({'error': message}, status=400)
                return HttpResponseBadRequest(message)
            treatment_meta = dict(existing_treatment.meta or {}) if existing_treatment and isinstance(existing_treatment.meta, dict) else {}
            def posted_coordinate(name, default):
                try:
                    value = request.POST.get(name, '')
                    return float(value) if value != '' else default
                except (TypeError, ValueError):
                    return default

            saved_a = treatment_meta.get('treatment_position_a') or {}
            saved_b = treatment_meta.get('treatment_position_b') or {}
            treatment_meta['treatment_position_a'] = {
                'x': posted_coordinate('treatment_position_a_x', saved_a.get('x', 3)),
                'y': posted_coordinate('treatment_position_a_y', saved_a.get('y', 1)),
            }
            treatment_meta['treatment_position_b'] = {
                'x': posted_coordinate('treatment_position_b_x', saved_b.get('x', 9)),
                'y': posted_coordinate('treatment_position_b_y', saved_b.get('y', 1)),
            }

            defaults = {
                'patient': patient,
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
                'total_pulses': cleaned.get('total_pulses'),
                'helmet_shift_cm': existing_treatment.helmet_shift_cm if existing_treatment else 6,
                'meta': treatment_meta,
                'treatment_notes': cleaned.get('treatment_notes',''),
                'motor_threshold': cleaned.get('mt_percent'),
                'intensity_percent': cleaned.get('intensity_percent'),
                'intensity': cleaned.get('intensity_percent'),
                'performer': request.user,
                'treatment_course': treatment_course,
                'course_number': course_number,
                'session_date': session_date,
                'slot': slot,
            }
            with transaction.atomic():
                Patient.objects.select_for_update().get(pk=patient.pk)
                latest_existing = TreatmentSession.objects.filter(
                    **session_scope,
                    session_date=session_date,
                    slot=slot,
                ).first()
                if latest_existing is None and not can_create_treatment_session(patient, course_number):
                    message = 'この患者・コースの治療予定は30回に達しているため、新規登録できません。'
                    if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.headers.get('Accept', '').startswith('application/json'):
                        return JsonResponse({'error': message}, status=400)
                    return HttpResponseBadRequest(message)
                if treatment_course is not None:
                    s, created = update_or_create_treatment_session_strict(
                        patient,
                        treatment_course,
                        course_number=course_number,
                        session_date=session_date,
                        slot=slot,
                        defaults=defaults,
                    )
                else:
                    s, created = update_or_create_treatment_session_legacy(
                        patient,
                        course_number,
                        session_date=session_date,
                        slot=slot,
                        defaults=defaults,
                    )

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

            sae_map = {
                'sae_seizure': 'seizure',
                'sae_finger_muscle': 'finger_muscle',
                'sae_syncope': 'syncope',
                'sae_mania': 'mania',
                'sae_suicide_attempt': 'suicide_attempt',
                'sae_other': 'other',
            }
            sae_event_types = [
                event_code for field_name, event_code in sae_map.items()
                if request.POST.get(field_name) == 'on'
            ]
            if sae_event_types:
                SeriousAdverseEvent.objects.update_or_create(
                    patient=patient,
                    course_number=course_number,
                    session=s,
                    defaults={
                        'event_types': sae_event_types,
                        'other_text': (request.POST.get('sae_other_text') or '').strip(),
                        'auto_snapshot': {
                            'date': session_date.isoformat(),
                            'mt_percent': s.mt_percent,
                            'intensity_percent': s.intensity_percent,
                            'frequency_hz': str(s.frequency_hz),
                            'train_seconds': str(s.train_seconds),
                            'train_count': s.train_count,
                            'total_pulses': s.total_pulses,
                            'coil_type': s.coil_type,
                            'target_site': s.target_site,
                        },
                    },
                )
            else:
                SeriousAdverseEvent.objects.filter(
                    patient=patient, course_number=course_number, session=s
                ).delete()

            if request.POST.get('action') == 'save_sae_report':
                def report_int(name):
                    try:
                        return int(request.POST.get(name)) if request.POST.get(name) else None
                    except (TypeError, ValueError):
                        return None
                def report_date(name):
                    value = request.POST.get(name, '')
                    return parse_date(value) if value else None
                diagnosis = request.POST.get('diagnosis', 'うつ病エピソード')
                diagnosis_category = {
                    'うつ病エピソード': 'depressive_episode',
                    '反復性うつ病性障害': 'recurrent_depressive_disorder',
                }.get(diagnosis, 'other')
                report, _ = AdverseEventReport.objects.update_or_create(
                    session=s,
                    defaults={
                        'event_types': sae_event_types,
                        'adverse_event_name': (request.POST.get('event_name') or '').strip(),
                        'onset_date': report_date('onset_date'),
                        'age': report_int('age'),
                        'sex': (request.POST.get('gender') or '').strip(),
                        'initials': (request.POST.get('initials') or '').strip(),
                        'diagnosis_category': diagnosis_category,
                        'diagnosis_other_text': (request.POST.get('diagnosis_other') or '').strip(),
                        'concomitant_meds_text': request.POST.get('concomitant_meds', ''),
                        'substance_intake_text': request.POST.get('substance_use', ''),
                        'seizure_history_flag': request.POST.get('seizure_history') == 'あり',
                        'seizure_history_date_text': request.POST.get('seizure_history_detail', ''),
                        'rmt_value': report_int('rmt'),
                        'intensity_value': report_int('intensity'),
                        'stimulation_site_category': 'other' if request.POST.get('site') == 'その他' else 'left_dlpfc',
                        'stimulation_site_other_text': request.POST.get('site', '') if request.POST.get('site') == 'その他' else '',
                        'treatment_course_number': report_int('treatment_number'),
                        'outcome_flags': [request.POST.get('outcome')] if request.POST.get('outcome') else [],
                        'outcome_date': report_date('outcome_date'),
                        'special_notes': request.POST.get('notes', ''),
                        'physician_comment': request.POST.get('doctor_comment', ''),
                        'prefilled_snapshot': {'source': 'treatment_add', 'session_date': session_date.isoformat()},
                    },
                )
                if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                    return JsonResponse({'status': 'success', 'report_id': report.id})

            # Check if print action is requested
            action = request.POST.get('action')
            if action == 'print':
                # Build print URL (PDF endpoint) and back_url for PRG
                back_params = {'date': d.isoformat()}
                if getattr(s, 'course_number', None):
                    back_params['course_number'] = s.course_number
                if dashboard_date:
                    back_params['dashboard_date'] = dashboard_date
                back_url = f"{reverse('rtms_app:treatment_add', args=[patient.id])}?{urlencode(back_params)}"

                print_params = {'back_url': back_url}
                if getattr(s, 'course_number', None):
                    print_params['course_number'] = s.course_number
                if dashboard_date:
                    print_params['dashboard_date'] = dashboard_date
                # Prefer server-side PDF endpoint
                pdf_url = f"{reverse('rtms_app:print:print_side_effect_check_pdf', args=[patient.id, s.id])}?{urlencode(print_params)}"
                html_url = f"{reverse('rtms_app:print:print_side_effect_check', args=[patient.id, s.id])}?{urlencode(print_params)}"

                # If this is an AJAX (fetch) request, return JSON with URLs so client can open PDF
                if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.headers.get('Accept','').startswith('application/json'):
                    return JsonResponse({'status': 'ok', 'pdf_url': pdf_url, 'html_url': html_url, 'session_id': s.id})

                # Otherwise, perform regular redirect to the HTML print page (PRG)
                return redirect(html_url)

            if action == 'skip':
                # Record snapshot of affected future planned sessions, then mark skip and shift
                try:
                    session_scope = {'treatment_course': s.treatment_course} if s.treatment_course else {
                        'patient': patient, 'course_number': s.course_number,
                    }
                    futures = list(TreatmentSession.objects.filter(**session_scope, session_date__gt=s.session_date).order_by('session_date', 'id'))
                    snapshot_list = []
                    for ts in futures:
                        snapshot_list.append({
                            'id': ts.id,
                            'session_date': ts.session_date.isoformat() if ts.session_date else None,
                            'date': ts.date.isoformat() if getattr(ts, 'date', None) else None,
                        })
                    snapshot = {
                        'affected_sessions': snapshot_list,
                        'patient_discharge_date': patient.discharge_date.isoformat() if getattr(patient, 'discharge_date', None) else None,
                        'skipped_session_id': s.id,
                        'skipped_session_date': s.session_date.isoformat() if s.session_date else None,
                    }

                    reason = (request.POST.get('skip_reason') or '').strip()
                    try:
                        sk = TreatmentSkip.objects.create(
                            treatment=s,
                            action_type='postpone',
                            effective_date=s.session_date,
                            reason=reason,
                            performed_by=request.user,
                            snapshot=snapshot,
                        )
                    except Exception:
                        pass

                    # Mark the session as skipped
                    s.status = 'skipped'
                    s.save(update_fields=['status'])

                    # Shift subsequent planned sessions forward by one treatment day each
                    try:
                        shift_future_sessions(patient, s.session_date, s.course_number)
                    except Exception:
                        pass
                except Exception as _outer_err:
                    # On any error, fallback to best-effort simple skip record
                    try:
                        s.status = 'skipped'
                        s.save(update_fields=['status'])
                        reason = (request.POST.get('skip_reason') or '').strip()
                        TreatmentSkip.objects.create(
                            treatment=s,
                            action_type='postpone',
                            effective_date=s.session_date,
                            reason=reason,
                            performed_by=request.user,
                        )
                    except Exception as _fallback_err:
                        pass
                    # Audit
                    try:
                        log_audit_action(patient, 'skip_treatment', 'TreatmentSession', s.id, summary=f"skipped via UI (fallback)")
                    except Exception:
                        pass

                # Redirect back to dashboard or treatment page
                if dashboard_date:
                    return redirect(build_url('dashboard', query={'date': dashboard_date}))
                return redirect(build_url('dashboard'))

            if action == 'cancel':
                # Cancel treatment: delete all future sessions and set discharge_date to today
                try:
                    session_scope = {'treatment_course': s.treatment_course} if s.treatment_course else {
                        'patient': patient, 'course_number': s.course_number,
                    }
                    futures = list(TreatmentSession.objects.filter(**session_scope, session_date__gte=s.session_date).order_by('session_date', 'id'))
                    snapshot_list = []
                    for ts in futures:
                        snapshot_list.append({
                            'id': ts.id,
                            'session_date': ts.session_date.isoformat() if ts.session_date else None,
                            'date': ts.date.isoformat() if getattr(ts, 'date', None) else None,
                        })

                    # Record current discharge_date for undo
                    snapshot = {
                        'affected_sessions': snapshot_list,
                        'patient_discharge_date': patient.discharge_date.isoformat() if getattr(patient, 'discharge_date', None) else None,
                        'cancelled_session_id': s.id,
                        'cancelled_session_date': s.session_date.isoformat() if s.session_date else None,
                    }

                    reason = (request.POST.get('skip_reason') or '').strip()

                    # Create TreatmentSkip record with action_type='cancel'
                    try:
                        sk = TreatmentSkip.objects.create(
                            treatment=s,
                            action_type='cancel',
                            effective_date=s.session_date,
                            reason=reason,
                            performed_by=request.user,
                            snapshot=snapshot,
                        )
                    except Exception as _e:
                        import traceback
                        try:
                            with open('/tmp/rtms_cancel_debug.log', 'a') as _f:
                                _f.write(f"CANCEL_SK_ERR: {repr(_e)}\n")
                                _f.write(traceback.format_exc() + "\n")
                        except Exception:
                            pass

                    # Delete all future sessions (from today onwards, including today)
                    for ts in futures:
                        ts.delete()

                    # Set patient discharge_date to today
                    patient.discharge_date = s.session_date
                    patient.save(update_fields=['discharge_date'])

                    # Audit
                    try:
                        log_audit_action(patient, 'cancel_treatment', 'TreatmentSession', s.id, summary=f"cancelled via UI by {request.user.username}")
                    except Exception:
                        pass

                except Exception as _outer_err:
                    # On any error, fallback to best-effort cancel record
                    try:
                        reason = (request.POST.get('skip_reason') or '').strip()
                        TreatmentSkip.objects.create(
                            treatment=s,
                            action_type='cancel',
                            effective_date=s.session_date,
                            reason=reason,
                            performed_by=request.user,
                        )
                        # Still try to set discharge_date
                        patient.discharge_date = s.session_date
                        patient.save(update_fields=['discharge_date'])
                    except Exception:
                        pass
                    # Audit
                    try:
                        log_audit_action(patient, 'cancel_treatment', 'TreatmentSession', s.id, summary=f"cancelled via UI (fallback)")
                    except Exception:
                        pass

                # Redirect back to dashboard or treatment page
                if dashboard_date:
                    return redirect(build_url('dashboard', query={'date': dashboard_date}))
                return redirect(build_url('dashboard'))

            # Normal save -> go back to dashboard
            focus_query = {'focus': d.isoformat()}
            if dashboard_date:
                focus_query['date'] = dashboard_date
            return redirect(build_url('dashboard', query=focus_query))
    else:
        # GET request - setup form with initial data
        initial_data = {
            'treatment_date': initial_date,
            'treatment_time': now.strftime('%H:%M'),
            'intensity_percent': 60,
            'mt_percent': 100,
            'frequency_hz': 18,
            'train_seconds': 2,
            'intertrain_seconds': 20,
            'train_count': 55,
            'total_pulses': 1980,
        }

        previous_session = TreatmentSession.objects.filter(
            **session_scope,
            session_date__lt=initial_date,
        ).filter(
            Q(intensity_percent__isnull=False) | Q(intensity__isnull=False),
        ).order_by('-session_date', '-date').first()

        if previous_session:
            previous_intensity = (
                previous_session.intensity_percent
                if previous_session.intensity_percent is not None
                else previous_session.intensity
            )
            if previous_intensity is not None:
                initial_data['intensity_percent'] = previous_intensity
            if previous_session.mt_percent is not None:
                initial_data['mt_percent'] = previous_session.mt_percent
        elif current_week_mapping and current_week_mapping.resting_mt is not None:
            initial_data['intensity_percent'] = current_week_mapping.resting_mt
            initial_data['mt_percent'] = 100

        form = TreatmentForm(initial=initial_data)

    # Load existing session and side effect data (DB is the source of truth)
    side_effect_rows = []
    side_effect_signature = ''
    side_effect_memo = ''
    side_effect_status = '未評価'
    existing_session = None

    # Find a treatment session for this patient on the initial_date using session_date field
    course_number = patient.course_number or 1
    existing_session = TreatmentSession.objects.filter(
        **session_scope, session_date=initial_date,
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
                'intensity_percent': existing_session.intensity_percent,
                'mt_percent': existing_session.mt_percent,
                'frequency_hz': existing_session.frequency_hz,
                'train_seconds': existing_session.train_seconds,
                'intertrain_seconds': existing_session.intertrain_seconds,
                'train_count': existing_session.train_count,
                'total_pulses': existing_session.total_pulses,
                'treatment_notes': existing_session.treatment_notes or '',
            })

        # Load side effect data
        sec = SideEffectCheck.objects.filter(session=existing_session).first()
        if sec:
            side_effect_rows = sec.rows or []
            side_effect_signature = sec.physician_signature or ''
            side_effect_memo = sec.memo or ''
            has_effect_value = any(
                any((row.get(key) or 0) for key in ('before', 'during', 'after', 'relatedness'))
                or (row.get('memo') or '').strip()
                for row in side_effect_rows if isinstance(row, dict)
            )
            if side_effect_memo == '副作用なし':
                side_effect_status = '副作用なし'
            elif has_effect_value or side_effect_memo.strip():
                side_effect_status = '副作用入力済み'

    sae_event_types_checked = {}
    sae_other_text_value = ''
    sae_report_exists = False
    sae_prefill = {}
    if existing_session:
        existing_sae = SeriousAdverseEvent.objects.filter(session=existing_session).first()
        if existing_sae:
            sae_event_types_checked = {
                f'sae_{event_code}': True for event_code in existing_sae.event_types
            }
            sae_other_text_value = existing_sae.other_text or ''
        sae_report_exists = AdverseEventReport.objects.filter(session=existing_session).exists()
        report = AdverseEventReport.objects.filter(session=existing_session).first()
        same_day_mapping = MappingSession.objects.filter(
            **mapping_scope, date=existing_session.session_date,
        ).first()
        if report:
            sae_prefill = {
                'event_name': report.adverse_event_name,
                'onset_date': report.onset_date.isoformat() if report.onset_date else '',
                'age': report.age or '', 'gender': report.sex, 'initials': report.initials,
                'concomitant_meds': report.concomitant_meds_text,
                'substance_use': report.substance_intake_text,
                'rmt': report.rmt_value or '', 'intensity': report.intensity_value or '',
                'treatment_number': report.treatment_course_number or '',
                'outcome': (report.outcome_flags or [''])[0],
                'diagnosis': report.diagnosis_other_text if report.diagnosis_category == 'other' else dict(AdverseEventReport.DIAGNOSIS_CHOICES).get(report.diagnosis_category, ''),
                'site': report.stimulation_site_other_text if report.stimulation_site_category == 'other' else dict(AdverseEventReport.SITE_CHOICES).get(report.stimulation_site_category, ''),
            }
        else:
            event_labels = dict(SeriousAdverseEvent.EVENT_CHOICES)
            selected_labels = [event_labels.get(code, code) for code in (existing_sae.event_types if existing_sae else [])]
            sae_prefill = {
                'event_name': '、'.join(selected_labels),
                'onset_date': existing_session.session_date.isoformat(),
                'age': patient.age,
                'gender': patient.get_gender_display(),
                'initials': '',
                'diagnosis': patient.diagnosis if patient.diagnosis in {
                    'うつ病エピソード', '反復性うつ病性障害',
                } else 'うつ病エピソード',
                'concomitant_meds': patient.medication_history or '',
                'rmt': same_day_mapping.resting_mt if same_day_mapping else '',
                'intensity': existing_session.mt_percent or '',
                'site': existing_session.target_site or '',
                'treatment_number': session_num or '',
            }

    # If no SideEffectCheck exists for this session/date, keep an empty array;
    # the widget renders default rows client-side.

    side_effect_rows_json = json.dumps(side_effect_rows, ensure_ascii=False)
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

    treatment_positions = {
        'a': {'x': 3, 'y': 1},
        'b': {'x': 9, 'y': 1},
    }
    if existing_session and isinstance(existing_session.meta, dict):
        saved_positions = {
            'a': existing_session.meta.get('treatment_position_a'),
            'b': existing_session.meta.get('treatment_position_b'),
        }
        if saved_positions['a'] and saved_positions['b']:
            treatment_positions = saved_positions


    return render(request, 'rtms_app/treatment_add.html', {
        'patient': patient,
        'course_number': course_number,
        'form': form,
        'current_week_mapping': current_week_mapping,
        'treatment_positions': treatment_positions,
        'mapping_alert': mapping_alert,
        'mode_switch_html': mode_switch_html,
        'initial_date': initial_date,
        'session_num': session_num,
        'week_num': week_num,
        'end_date_est': end_date_est,
        'start_date': patient.first_treatment_date,
        'dashboard_date': dashboard_date,
        'alert_msg': alert_msg,
        'instruction_msg': instruction_msg,
        'judgment_info': judgment_info,
        'week3_status': week3_status,
        'hamd3w_available': hamd_eval['available'],
        'hamd3w_score': hamd_eval['score'],
        'hamd3w_improvement_pct': hamd_eval['improvement_pct'],
        'hamd3w_status': hamd_eval['status'],
        'hamd_eval': hamd_eval,
        'week3_assessment_url': week3_assessment_url,
        'side_effect_items': side_effect_items,
        'side_effect_rows_json': side_effect_rows_json,
        'side_effect_signature': side_effect_signature,
        'side_effect_memo': side_effect_memo,
        'sae_event_types_checked': sae_event_types_checked,
        'sae_other_text_value': sae_other_text_value,
        'session': existing_session,
        'sae_report_exists': sae_report_exists,
        'sae_prefill': sae_prefill,
        'side_effect_status': side_effect_status,
        'print_options': print_options,
        'today': timezone.now().date(),
        'initial_timing_display': '',
        'can_view_audit': can_view_audit(request.user),
    })


@login_required
def treatment_skip_list(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    requested_course_number = request.GET.get('course_number')
    try:
        course_number = int(requested_course_number) if requested_course_number else (patient.course_number or 1)
    except (TypeError, ValueError):
        return HttpResponseBadRequest('クール番号が不正です')
    treatment_course = resolve_treatment_course(patient, course_number=course_number)
    session_scope = {'treatment__treatment_course': treatment_course} if treatment_course else {
        'treatment__patient': patient, 'treatment__course_number': course_number,
    }
    skips = TreatmentSkip.objects.filter(**session_scope).select_related(
        'treatment', 'performed_by', 'treatment__treatment_course',
    ).order_by('-created_at')
    return render(request, 'rtms_app/skip_list.html', {
        'patient': patient, 'course_number': course_number, 'skips': skips,
    })


@login_required
def treatment_skip_undo(request, skip_id):
    if request.method != 'POST':
        return redirect('rtms_app:dashboard')
    # ensure model is available in this scope (avoid NameError if module import resolution varies)
    # import model dynamically to avoid any name-resolution issues during import-time quirks
    sk = get_object_or_404(__import__('rtms_app.models', fromlist=['TreatmentSkip']).TreatmentSkip, pk=skip_id)
    patient = sk.treatment.patient
    try:
        # If a snapshot exists, restore affected sessions and patient discharge_date
        snap = sk.snapshot or {}
        affected = snap.get('affected_sessions') if isinstance(snap, dict) else None

        if affected:
            # For cancel action: recreate deleted sessions
            if sk.action_type == 'cancel':
                for item in affected:
                    try:
                        ts_id = item.get('id')
                        # Try to get the existing session first
                        ts = TreatmentSession.objects.filter(pk=ts_id).first()
                        if not ts:
                            # Session was deleted, we can't fully recreate it
                            # Log this but don't fail
                            continue

                        orig_sd = item.get('session_date')
                        orig_dt = item.get('date')
                        if orig_sd:
                            from datetime import date as _d, datetime as _dt
                            ts.session_date = _dt.fromisoformat(orig_sd).date() if 'T' in orig_sd else _d.fromisoformat(orig_sd)
                        if orig_dt:
                            try:
                                ts.date = _dt.fromisoformat(orig_dt)
                            except Exception:
                                try:
                                    ts.date = _dt.combine(_d.fromisoformat(orig_dt), _dt.min.time())
                                except Exception:
                                    pass
                        ts.save(update_fields=['session_date', 'date'])
                    except Exception:
                        continue
            else:
                # For postpone action: restore original session dates
                for item in affected:
                    try:
                        ts = TreatmentSession.objects.filter(pk=item.get('id')).first()
                        if not ts:
                            continue
                        orig_sd = item.get('session_date')
                        orig_dt = item.get('date')
                        if orig_sd:
                            from datetime import date as _d, datetime as _dt
                            # parse date
                            ts.session_date = _dt.fromisoformat(orig_sd).date() if 'T' in orig_sd else _d.fromisoformat(orig_sd)
                        if orig_dt:
                            # parse datetime
                            try:
                                ts.date = _dt.fromisoformat(orig_dt)
                            except Exception:
                                # fallback to date only
                                try:
                                    ts.date = _dt.combine(_d.fromisoformat(orig_dt), _dt.min.time())
                                except Exception:
                                    pass
                        ts.save(update_fields=['session_date', 'date'])
                    except Exception:
                        continue

        # restore patient discharge_date if snapshot provided
        try:
            pdd = snap.get('patient_discharge_date') if isinstance(snap, dict) else None
            if pdd:
                from datetime import date as _d
                patient.discharge_date = _d.fromisoformat(pdd)
                patient.save(update_fields=['discharge_date'])
        except Exception:
            pass

        # restore status of the skipped/cancelled session to planned
        t = sk.treatment
        t.status = 'planned'
        t.save(update_fields=['status'])

        # mark skip as undone (preserve record for audit)
        try:
            sk.undone_by = request.user
            sk.undone_at = timezone.now()
            sk.save(update_fields=['undone_by', 'undone_at'])
        except Exception:
            pass
        try:
            action_str = 'cancel' if sk.action_type == 'cancel' else 'skip'
            log_audit_action(patient, f'undo_{action_str}', 'TreatmentSkip', skip_id, summary=f"undo {action_str} via UI by {request.user.username}")
        except Exception:
            pass
    except Exception:
        pass
    # redirect back to patient skips page
    return redirect('rtms_app:treatment_skip_list', patient_id=patient.id)

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


def assessment_shortcut(request, patient_id, timing):
    """Keep the short assessment URL compatible with the add endpoint."""
    target = reverse('rtms_app:assessment_add', args=[patient_id, timing])
    query = request.META.get('QUERY_STRING', '')
    return redirect(f"{target}?{query}" if query else target)


def assessment_add(request, patient_id, timing):
    """Canonical assessment entry point: show the scale selection Hub."""
    return assessment_hub(request, patient_id, timing)


def assessment_add_legacy(request, patient_id, timing):
    patient = get_object_or_404(Patient, pk=patient_id)
    treatment_course = resolve_treatment_course(
        patient,
        course_number=request.GET.get('course_number') or request.POST.get('course_number'),
    )
    course_number = treatment_course.course_number if treatment_course else patient.course_number or 1
    dashboard_date = request.GET.get('dashboard_date')
    from_page = request.GET.get('from')  # 'clinical_path' などを想定

    # Validate timing against model choices to prevent tampering
    allowed = [c[0] for c in Assessment.TIMING_CHOICES]
    if timing not in allowed:
        return HttpResponse(status=400)

    history = get_assessments_ordered(
        patient, treatment_course=treatment_course, course_number=course_number,
    )

    # hamd_items with anchor text from HAMD_ANCHORS
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

    # Calculate assessment window
    window_start, window_end = get_assessment_window(patient, timing)


    existing_assessment = get_latest_assessment(
        patient, timing, treatment_course=treatment_course, course_number=course_number,
    )

    # Determine initial date priority:
    # 1) explicit `date` GET param (calendar/dashboard click)
    # 2) `dashboard_date` GET param
    # 3) fallback to today
    # Later, if an existing assessment exists and no explicit GET date was provided,
    # prefer the saved assessment date so saved dates are persistent.
    initial_date = timezone.now().date()
    # Accept multiple GET param names for compatibility
    date_param = request.GET.get('date') or request.GET.get('dashboard_date') or request.GET.get('selected_date') or request.GET.get('calendar_date')
    if date_param:
        try:
            initial_date = datetime.datetime.strptime(date_param, '%Y-%m-%d').date()
        except:
            pass

    # If there is an existing saved assessment and the user didn't explicitly pass a date,
    # prefer the saved assessment date so it remains fixed after saving.
    if existing_assessment and not date_param:
        initial_date = existing_assessment.date

    # Use `default_date` (date object) in context and for POST fallbacks
    default_date = initial_date

    # Get timing display name
    timing_display = dict(Assessment.TIMING_CHOICES).get(timing, timing)

    if request.method == 'POST':
        try:
            # date/timing: if the form left date empty, fall back to default_date
            date_str = (request.POST.get('date') or '').strip()
            try:
                date = datetime.date.fromisoformat(date_str) if date_str else default_date
            except Exception:
                date = default_date

            timing_post = request.POST.get('timing') or timing
            if timing_post not in allowed:
                timing_post = timing

            # scores from hidden inputs
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

            # Upsert assessment by natural key: patient + course_number + timing + type
            save_hamd = save_assessment_hamd if treatment_course is not None else save_assessment_hamd_legacy
            assessment, created = save_hamd(
                patient=patient,
                course_number=course_number,
                timing=timing_post,
                date=date,
                scores=scores,
                note=note,
                treatment_course=treatment_course,
            )

            # Ajax / modal 保存時は JSON を返す
            if request.headers.get('x-requested-with') == 'XMLHttpRequest' or request.GET.get('modal') == '1':
                # total_17 を返す（first_visitのサマリー更新で使う想定）
                redirect_url = f"{reverse('rtms_app:patient_first_visit', args=[patient.id])}?dashboard_date={dashboard_date}" if dashboard_date else reverse('rtms_app:patient_first_visit', args=[patient.id])
                return JsonResponse({
                    'status': 'success',
                    'id': assessment.id,
                    'total_17': assessment.total_score_17,
                    'redirect_url': redirect_url,
                })

            # ---- 戻りURLは build_url に統一（/path/&focus=... を絶対に作らない） ----
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

    # JSON-serialize existing scores for safe embedding in JS
    existing_scores_json = json.dumps(existing_assessment.scores) if existing_assessment and getattr(existing_assessment, 'scores', None) else '{}'

    modal_mode = request.GET.get('modal') == '1'

    ctx = {
        'patient': patient,
        'default_date': default_date,
        'dashboard_date': dashboard_date,
        'initial_timing': timing,
        'initial_timing_display': timing_display,
        'existing_assessment': existing_assessment,
        'history': history,
        'hamd_items_left': hamd_items_left,
        'hamd_items_right': hamd_items_right,
        'window_start': window_start,
        'window_end': window_end,
        'recommendation': None,
        'modal_mode': modal_mode,
    }
    # Embed existing scores for templates expecting `existing_scores`
    ctx['existing_scores'] = existing_scores_json
    ctx['can_view_audit'] = can_view_audit(request.user)
    # Scale selection for future extensibility: currently only HAM-D
    ctx['scale_template'] = 'rtms_app/assessment/scales/hamd.html'
    ctx['scale_name'] = 'HAM-D'
    ctx['existing_note'] = existing_assessment.note if existing_assessment else ''
    # baseline score for improvement calculations; may be None
    baseline_assess = get_latest_assessment(
        patient, 'baseline', treatment_course=treatment_course, course_number=course_number,
    )
    ctx['baseline_score_17'] = baseline_assess.total_score_17 if baseline_assess else None
    return render(request, 'rtms_app/assessment_add.html', ctx)

def assessment_week4(request, patient_id):
    """
    4週経過後HAM-D評価用のラッパービュー
    """
    return assessment_add(request, patient_id, timing='week4')



def assessment_hub_entry(request, patient_id, timing, *args, **kwargs):
    """Compatibility entry point that forwards to `assessment_hub`.

    Some decorators or URL dispatchers may pass keyword args unexpectedly;
    make a simple entry function with an explicit signature to avoid
    TypeError when Django forwards URL kwargs.
    """
    return assessment_hub(request, patient_id, timing)


def _build_month_calendar(year, month, is_print=False):
    today = timezone.localdate()
    first_day = date(year, month, 1)
    last_day = date(year, month, pycalendar.monthrange(year, month)[1])
    grid_start = first_day - timedelta(days=first_day.weekday())
    grid_end = last_day + timedelta(days=6 - last_day.weekday())

    patients = list(Patient.objects.filter(
        admission_date__isnull=False,
        admission_date__lte=grid_end,
    ).filter(Q(discharge_date__isnull=True) | Q(discharge_date__gte=grid_start)))
    all_treatments = list(TreatmentSession.objects.select_related('patient').order_by(
        'patient_id', 'course_number', 'session_date', 'date', 'id',
    ))
    treatment_number_by_id = {}
    grouped_treatments = {}
    for session in all_treatments:
        grouped_treatments.setdefault((session.patient_id, session.course_number), []).append(session)
    for (patient_id, course_number), sessions in grouped_treatments.items():
        patient = sessions[0].patient
        treatment_number_by_id.update(
            get_treatment_session_number_map(patient, course_number)
        )
    treatments = [
        session for session in all_treatments
        if session.id in treatment_number_by_id
        and session.status != 'skipped'
        and grid_start <= session.session_date <= grid_end
    ]

    def surname(patient):
        name = (patient.name or '').strip()
        return name.split()[0] if name else ''

    inpatient_by_date = {}
    for p in patients:
        current = max(grid_start, p.admission_date)
        end = min(grid_end, p.discharge_date) if p.discharge_date else grid_end
        while current <= end:
            inpatient_by_date.setdefault(current, set()).add(p.id)
            current += timedelta(days=1)

    treatment_by_date = {}
    for session in treatments:
        treatment_by_date.setdefault(session.session_date, []).append(session)

    estimated_discharge_by_date = {}
    for p in Patient.objects.filter(
        first_treatment_date__isnull=False,
        discharge_date__isnull=True,
        first_treatment_date__lte=grid_end,
    ):
        dates = generate_treatment_dates(p.first_treatment_date, total=30, holidays=JP_HOLIDAYS)
        if dates and grid_start <= dates[-1] <= grid_end:
            estimated_discharge_by_date.setdefault(dates[-1], []).append(p)

    weeks = []
    current = grid_start
    current_week = []
    while current <= grid_end:
        day_date = current
        events = []
        for p in patients:
            if p.admission_date == day_date:
                events.append({'kind': 'admission', 'label': f'入院（{surname(p)}）', 'url': build_url('admission_procedure', [p.id])})
            if p.discharge_date == day_date:
                events.append({'kind': 'discharge', 'label': f'退院（{surname(p)}）', 'url': build_url('patient_home', [p.id])})
        for session in treatment_by_date.get(day_date, []):
            events.append({
                'kind': 'treatment',
                'label': f'rTMS治療（{surname(session.patient)}＃{treatment_number_by_id[session.id]}回）',
                'url': build_url('treatment_add', [session.patient_id], query={'date': day_date.isoformat()}),
                'is_planned': session.status == 'planned',
            })
        for p in estimated_discharge_by_date.get(day_date, []):
            events.append({'kind': 'discharge', 'label': f'退院（{surname(p)}）', 'url': build_url('patient_home', [p.id]), 'is_planned': True})
        events.sort(key=lambda e: (e['kind'] != 'admission', e['kind'] != 'treatment'))
        visible_limit = 8
        current_week.append({
            'date': day_date,
            'weekday': day_date.weekday(),
            'is_holiday': is_holiday(day_date),
            'is_current_month': day_date.month == month,
            'holiday_name': '',
            'day_url': build_url('dashboard', query={'date': day_date.isoformat()}),
            'events_visible': events[:visible_limit],
            'events_hidden_count': max(0, len(events) - visible_limit),
            'inpatient_count': len(inpatient_by_date.get(day_date, set())),
            'rtms_count': len(treatment_by_date.get(day_date, [])),
        })
        if day_date.weekday() == 6:
            weeks.append(current_week)
            current_week = []
        current += timedelta(days=1)
    previous = date(year, month, 1) - timedelta(days=1)
    next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    all_days = [d for week in weeks for d in week]
    return {
        'year': year,
        'month': month,
        'weeks': weeks,
        'prev_year': previous.year,
        'prev_month': previous.month,
        'next_year': next_month.year,
        'next_month': next_month.month,
        'today': today,
        'peak_inpatients': max((d['inpatient_count'] for d in all_days), default=0),
        'peak_rtms': max((d['rtms_count'] for d in all_days), default=0),
    }


@login_required
def calendar_month_view(request):
    today = timezone.localdate()
    try:
        year = int(request.GET.get('year', today.year))
        month = int(request.GET.get('month', today.month))
        date(year, month, 1)
    except (TypeError, ValueError):
        year, month = today.year, today.month
    return render(request, 'rtms_app/calendar_month.html', _build_month_calendar(year, month))


@login_required
def calendar_month_print_view(request):
    today = timezone.localdate()
    try:
        year = int(request.GET.get('year', today.year))
        month = int(request.GET.get('month', today.month))
        date(year, month, 1)
    except (TypeError, ValueError):
        year, month = today.year, today.month
    return render(request, 'rtms_app/print/calendar_month.html', _build_month_calendar(year, month, is_print=True))


@login_required
def adverse_event_report_print_preview(request):
    context = {
        'checked_events': request.POST.getlist('checked_events[]'),
        'event_name': request.POST.get('event_name', ''),
        'onset_date': request.POST.get('onset_date', ''),
        'age': request.POST.get('age', ''),
        'gender': request.POST.get('gender', ''),
        'initials': request.POST.get('initials', ''),
        'diagnosis': request.POST.get('diagnosis', ''),
        'concomitant_meds': request.POST.get('concomitant_meds', ''),
        'substance_use': request.POST.get('substance_use', ''),
        'rmt': request.POST.get('rmt', ''),
        'intensity': request.POST.get('intensity', ''),
        'site': request.POST.get('site', ''),
        'treatment_number': request.POST.get('treatment_number', ''),
        'outcome': request.POST.get('outcome', ''),
        'outcome_date': request.POST.get('outcome_date', ''),
        'notes': request.POST.get('notes', ''),
        'doctor_comment': request.POST.get('doctor_comment', ''),
        'report_date': timezone.localdate().strftime('%Y年%m月%d日'),
        'facility_name': '笠寺精治寮病院',
        'facility_phone': '052-821-1229',
    }
    return render(request, 'rtms_app/print/adverse_event_report.html', context)


@login_required
def adverse_event_report_print(request, session_id):
    session = get_object_or_404(TreatmentSession, pk=session_id)
    report = getattr(session, 'adverse_event_report', None)
    context = {'session': session, 'patient': session.patient, 'report': report}
    return render(request, 'rtms_app/print/adverse_event_report_db.html', context)


def assessment_hub(request, patient_id, timing):
    """Show all configured scales in a timing-by-scale assessment matrix."""
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date') or request.POST.get('dashboard_date')
    modal_mode = request.GET.get('modal') == '1'
    from_page = request.GET.get('from') or request.POST.get('from')

    allowed = [c[0] for c in Assessment.TIMING_CHOICES] + ['post'] + [f'tinkertory_{i}' for i in range(1, 8)]
    if timing not in allowed:
        return HttpResponse(status=400)

    timing_labels = dict(Assessment.TIMING_CHOICES)
    research_timing_labels = {'baseline': '治療前', 'post': '治療後'}
    ot_timing_labels = {
        'baseline': '治療前',
        'post': '治療後',
        **{f'tinkertory_{index}': staff_timing_label(f'tinkertory_{index}') for index in range(1, 8)},
    }
    date_param = (
        request.GET.get('date')
        or request.GET.get('dashboard_date')
        or request.GET.get('selected_date')
        or request.GET.get('calendar_date')
    )
    treatment_course = resolve_treatment_course(
        patient,
        course_number=request.GET.get('course_number') or request.POST.get('course_number'),
    )
    course_number = treatment_course.course_number if treatment_course else patient.course_number or 1
    assessment_scope = {'treatment_course': treatment_course} if treatment_course else {
        'patient': patient, 'course_number': course_number,
    }
    matrix_timings = {
        'hamd': ['baseline', 'week3', 'week4', 'week6'],
        'research': ['baseline', 'post'],
    }
    simple_order = ['phq9', 'sass-j', 'bdi-ii', 'sds', 'stai-trait', 'stai-state', 'dai-10']

    if request.method == 'POST' and timing in ('baseline', 'post'):
        try:
            assessed_date = parse_date(request.POST.get('date', '')) or timezone.localdate()
            for cell_timing in ('baseline', 'post'):
                for code in SIMPLE_SCALE_CODES:
                    scale = ScaleDefinition.objects.filter(code=code, is_active=True).first()
                    if scale is None:
                        continue
                    value = parse_simple_score(request.POST.get(f'score_{code}_{cell_timing}'))
                    if value is None:
                        AssessmentRecord.objects.filter(
                            **assessment_scope, timing=cell_timing, scale=scale,
                        ).delete()
                        continue
                    save_record = save_assessment_record if treatment_course is not None else save_assessment_record_legacy
                    save_record(
                        patient=patient, course_number=course_number, timing=cell_timing,
                        scale=scale, date=assessed_date,
                        scores={'score': value},
                        note='',
                        defaults_override={
                            'status_label': '入力済',
                            'treatment_course': treatment_course,
                        },
                    )
            redirect_query = {'course_number': course_number}
            if dashboard_date:
                redirect_query['dashboard_date'] = dashboard_date
            return redirect(build_url(
                'assessment_hub', args=[patient.id, timing], query=redirect_query,
            ))
        except ValueError as exc:
            return HttpResponseBadRequest(str(exc))

    def ensure_placeholder_scale(code, name):
        scale, _ = ScaleDefinition.objects.get_or_create(
            code=code,
            defaults={'name': name, 'is_active': True},
        )
        if scale.name != name or not scale.is_active:
            scale.name = name
            scale.is_active = True
            scale.save(update_fields=['name', 'is_active'])
        return scale

    configured_scales = list(
        ScaleDefinition.objects.filter(is_active=True)
        .order_by('code')
    )
    scale_orders = {
        row['scale_id']: row['display_order']
        for row in TimingScaleConfig.objects.filter(is_enabled=True).values('scale_id', 'display_order')
    }
    configured_scales.sort(key=lambda scale: (scale_orders.get(scale.id, 999), scale.code))

    def cell_for(scale, cell_timing):
        existing = get_assessment_by_timing_with_fallback(
            patient, cell_timing, scale, course_number,
            treatment_course=treatment_course,
        )
        query = {'from': 'assessment_hub'}
        if dashboard_date:
            query['dashboard_date'] = dashboard_date
        if date_param:
            query['date'] = date_param
        if modal_mode or scale.code in {'hamd', 'who-das', 'bacs', 'copm', '6mwt', 'tinkertory-test'}:
            query['modal'] = '1'
        total = getattr(existing, 'total_score_17', None) if existing else None
        existing_scores = getattr(existing, 'scores', {}) if existing else {}
        if scale.code in SIMPLE_SCALE_CODES:
            status_text = str(existing_scores.get('score')) if existing_scores.get('score') is not None else '未評価'
        elif existing and scale.code == 'hamd' and total is not None:
            status_text = f'入力済 {total}点'
        elif existing:
            status_text = summary_for_record(scale.code, existing_scores)
        else:
            status_text = '未評価'
        return {
            'is_done': existing is not None,
            'status_text': status_text,
            'is_simple': scale.code in SIMPLE_SCALE_CODES and cell_timing in ('baseline', 'post'),
            'input_name': f'score_{scale.code}_{cell_timing}' if cell_timing in ('baseline', 'post') else '',
            'input_value': existing_scores.get('score') if existing else '',
            'tab_index': (simple_order.index(scale.code) + 1 if cell_timing == 'baseline' else len(simple_order) + simple_order.index(scale.code) + 1)
                if scale.code in SIMPLE_SCALE_CODES and cell_timing in ('baseline', 'post') else '',
            'is_detail': scale.code in {'who-das', 'bacs', 'copm', '6mwt', 'tinkertory-test'},
            'is_hamd': scale.code == 'hamd',
            'timing_label': timing_labels.get(cell_timing, cell_timing),
            'date': getattr(existing, 'date', None),
            'url': build_url('assessment_scale', args=[patient.id, cell_timing, scale.code], query=query),
        }

    hamd_scales = [scale for scale in configured_scales if scale.code == 'hamd']
    ot_only_scale_codes = {'who-das', 'bacs', 'copm', '6mwt', 'tinkertory-test'}
    research_scales = [
        scale for scale in configured_scales
        if scale.code != 'hamd' and scale.code not in ot_only_scale_codes
    ]

    ot_standard_scales = [
        ensure_placeholder_scale('who-das', 'WHO-DAS'),
        ensure_placeholder_scale('bacs', 'BACS'),
        ensure_placeholder_scale('copm', 'COPM'),
        ensure_placeholder_scale('6mwt', '6MWT'),
    ]
    tinkertory_scale = ensure_placeholder_scale('tinkertory-test', 'Tinkertory Test')

    def build_section(title, scales, section_timings):
        return {
            'title': title,
            'columns': [
                {'code': item, 'label': research_timing_labels.get(item, timing_labels.get(item, item))}
                for item in section_timings
            ],
            'rows': [
                {
                    'name': scale.name,
                    'code': scale.code,
                    'cells': [cell_for(scale, item) for item in section_timings],
                }
                for scale in scales
            ],
        }

    ot_pre_post_columns = [
        {'code': 'baseline', 'label': '治療前'},
        {'code': 'post', 'label': '治療後'},
    ]
    ot_pre_post_table = {
        'title': 'OT研究用評価尺度',
        'columns': ot_pre_post_columns,
        'rows': [
            {
                'name': scale.name,
                'code': scale.code,
                'cells': [cell_for(scale, item) for item in ['baseline', 'post']],
            }
            for scale in ot_standard_scales
        ],
    }

    tinkertory_table = {
        'title': 'Tinkertory Test',
        'columns': [
            {'code': f'tinkertory_{i}', 'label': ot_timing_labels[f'tinkertory_{i}']}
            for i in range(1, 8)
        ],
        'rows': [
            {
                'name': 'Tinkertory Test',
                'code': tinkertory_scale.code,
                'cells': [
                    cell_for(tinkertory_scale, f'tinkertory_{i}')
                    for i in range(1, 8)
                ],
            }
        ],
    }

    ot_research_section = {
        'title': 'OT研究用評価尺度',
        'tables': [ot_pre_post_table, tinkertory_table],
    }

    matrix_sections = [
        build_section('HAM-D', hamd_scales, matrix_timings['hamd']),
        build_section('研究用評価尺度', research_scales, matrix_timings['research']),
        ot_research_section,
    ]

    ctx = {
        'patient': patient,
        'dashboard_date': dashboard_date,
        'initial_timing': timing,
        'initial_timing_display': timing_labels.get(timing, timing),
        'matrix_sections': matrix_sections,
        'from_page': from_page,
        'can_view_audit': can_view_audit(request.user),
        'modal_mode': modal_mode,
    }

    # Use different template for modal vs full-page
    if modal_mode:
        return render(request, 'rtms_app/assessment/hub_modal.html', ctx)
    else:
        return render(request, 'rtms_app/assessment/hub.html', ctx)


def assessment_hub_redirect(request, patient_id, initial_timing):
    """Redirect /assessment/hub/<initial_timing>/ to the canonical assessment_add URL.

    Use positional args to avoid reverse kwarg name mismatches between
    the hub URL (initial_timing) and the assessment_add URL (timing).
    """
    qs = request.META.get('QUERY_STRING', '')
    target = reverse('rtms_app:assessment_add', args=[patient_id, initial_timing])
    if qs:
        target = f"{target}?{qs}"
    return redirect(target)


def assessment_scale_form(request, patient_id, timing, scale_code):
    """Render assessment scale form (currently supports HAM-D only)."""
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date') or request.POST.get('dashboard_date')
    modal_mode = request.GET.get('modal') == '1'
    from_page = request.GET.get('from') or request.POST.get('from')

    allowed = [c[0] for c in Assessment.TIMING_CHOICES] + ['post'] + [f'tinkertory_{i}' for i in range(1, 8)]
    if timing not in allowed:
        return HttpResponse(status=400)

    detail_scale_names = {
        'who-das': 'WHO-DAS', 'bacs': 'BACS', 'copm': 'COPM', '6mwt': '6MWT',
        'tinkertory-test': 'Tinkertoy Test',
    }
    if scale_code in detail_scale_names:
        scale, _created = ScaleDefinition.objects.get_or_create(
            code=scale_code,
            defaults={'name': detail_scale_names[scale_code], 'is_active': True},
        )
    else:
        scale = get_object_or_404(ScaleDefinition, code=scale_code)

    timing_display = {
        **dict(Assessment.TIMING_CHOICES),
        **{
            f'tinkertory_{i}': f'{i}回目' for i in range(1, 8)
        },
    }.get(timing, timing)
    window_start, window_end = get_assessment_window(patient, timing)

    treatment_course = resolve_treatment_course(
        patient,
        course_number=request.GET.get('course_number') or request.POST.get('course_number'),
    )
    course_number = treatment_course.course_number if treatment_course else patient.course_number or 1
    assessment_scope = {'treatment_course': treatment_course} if treatment_course else {
        'patient': patient, 'course_number': course_number,
    }

    record = (
        AssessmentRecord.objects.filter(
            **assessment_scope,
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
                **assessment_scope,
                timing=timing,
                type='HAM-D',
            )
            .order_by('-date')
            .first()
        )

    # Determine initial date priority
    import datetime
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
                scores = build_detail_scores(request, scale.code)
                save_record = save_assessment_record if treatment_course is not None else save_assessment_record_legacy
                record, _created = save_record(
                    patient=patient,
                    course_number=course_number,
                    timing=timing,
                    scale=scale,
                    date=assessed_date,
                    scores=scores,
                    note=note,
                    defaults_override={
                        'status_label': '入力済',
                        'treatment_course': treatment_course,
                    },
                )
                if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                    return JsonResponse({
                        'status': 'success',
                        'id': record.id,
                        'message': '保存しました。',
                        'summary': summary_for_record(scale.code, record.scores),
                        'date': record.date.isoformat(),
                    })
                if from_page == 'assessment_hub':
                    return redirect(build_url('assessment_hub', args=[patient.id, timing], query={'dashboard_date': dashboard_date} if dashboard_date else None))
                return redirect(build_url('dashboard', query={'date': dashboard_date} if dashboard_date else None))

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
            }

            # Calculate improvement/status for non-baseline
            if timing != 'baseline':
                from .assessment_rules import compute_improvement_rate, classify_response_status
                baseline_obj = Assessment.objects.filter(
                    **assessment_scope, timing='baseline', type='HAM-D'
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

            save_record = save_assessment_record if treatment_course is not None else save_assessment_record_legacy
            new_record, _created = save_record(
                patient=patient,
                course_number=course_number,
                timing=timing,
                scale=scale,
                date=assessed_date,
                scores=scores,
                note=note,
                defaults_override={
                    k: v for k, v in rec_defaults.items()
                    if k in ['improvement_rate_17', 'status_label']
                } | {'treatment_course': treatment_course},
            )

            # Keep legacy table in sync
            if scale.code == 'hamd':
                save_hamd = save_assessment_hamd if treatment_course is not None else save_assessment_hamd_legacy
                legacy_obj, _legacy_created = save_hamd(
                    patient=patient,
                    course_number=course_number,
                    timing=timing,
                    date=assessed_date,
                    scores=scores,
                    note=note,
                    treatment_course=treatment_course,
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

            if from_page == 'assessment_hub':
                return redirect(build_url('assessment_hub', args=[patient.id, timing], query={'dashboard_date': dashboard_date} if dashboard_date else None))

            if dashboard_date:
                return redirect(build_url('dashboard', query={'date': dashboard_date}))
            return redirect(build_url('dashboard'))

        except Exception as e:
            if request.headers.get('x-requested-with') == 'XMLHttpRequest':
                return JsonResponse({'status': 'error', 'message': f'保存に失敗しました。: {str(e)}'}, status=400)
            return HttpResponse(f'保存に失敗しました。: {str(e)}', status=400)

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

        ctx = {
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
            'from_page': from_page,
            'can_view_audit': can_view_audit(request.user),
            'modal_mode': modal_mode,
        }

        # Use modal template if modal_mode
        template = 'rtms_app/assessment/scales/hamd_modal.html' if modal_mode else 'rtms_app/assessment/scales/hamd.html'

        response = render(request, template, ctx)
        return response

    return render(request, 'rtms_app/assessment/scales/detail_modal.html' if modal_mode else 'rtms_app/assessment/scales/detail.html', {
        'patient': patient,
        'dashboard_date': dashboard_date,
        'scale': scale,
        'scale_name': scale.name,
        'scale_code': scale.code,
        'initial_timing': timing,
        'initial_timing_display': staff_timing_label(timing),
        'window_start': window_start,
        'window_end': window_end,
        'default_date': default_date,
        'existing_note': existing_note,
        'existing_scores': existing_scores,
        'from_page': from_page,
        'detail_fields': DETAIL_FIELDS.get(scale.code, []),
        'copm_fields': COPM_FIELDS,
        'six_mwt_fields': SIX_MWT_VITAL_FIELDS + SIX_MWT_SUBJECTIVE_FIELDS,
        'tinkertoy_fields': TINKERTOY_FIELDS,
        'detail_context': detail_form_context(scale.code, existing_scores),
    })

@login_required
def patient_summary_view(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    dashboard_date = request.GET.get('dashboard_date')
    treatment_course = resolve_treatment_course(
        patient,
        course_number=request.GET.get('course_number') or request.POST.get('course_number'),
    )
    course_number = treatment_course.course_number if treatment_course else patient.course_number or 1

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


    summary_scope = {'treatment_course': treatment_course} if treatment_course else {
        'patient': patient, 'course_number': course_number,
    }
    sessions = TreatmentSession.objects.filter(**summary_scope).order_by('date')
    assessments = get_assessments_ordered(patient, treatment_course=treatment_course, course_number=course_number)
    test_scores = assessments
    score_admin = assessments.first()
    score_w3 = get_latest_assessment(patient, 'week3', treatment_course=treatment_course, course_number=course_number)
    score_w6 = get_latest_assessment(patient, 'week6', treatment_course=treatment_course, course_number=course_number)

    timing_order = [
        ('baseline', '治療前'),
        ('week3', '3週'),
        ('week4', '4週'),
        ('week6', '6週'),
    ]
    latest_by_timing = {
        t: get_latest_assessment(
            patient, t, treatment_course=treatment_course, course_number=course_number,
        ) for t, _ in timing_order
    }
    baseline_obj = latest_by_timing.get('baseline')
    baseline_17 = getattr(baseline_obj, 'total_score_17', None)

    # Use shared service builders so screen and print use identical data
    from .services.course_summary_service import build_treatment_session_display, build_assessment_trend

    trend_cols = build_assessment_trend(
        patient, timings=[t for t, _ in timing_order], treatment_course=treatment_course,
    )

    # build history list using service (includes mt, stim_pct_mt and computed output_pct)
    history_list = build_treatment_session_display(
        patient, course_number=course_number, treatment_course=treatment_course,
    )
    # side effects summary
    side_effects_all = [h.get('se') for h in history_list if h.get('se') and h.get('se') != 'なし']
    if side_effects_all:
        # flatten and unique
        parts = []
        for s in side_effects_all:
            parts.extend([p for p in s.split('、') if p])
        side_effects_summary = ", ".join(sorted(set(parts))) if parts else "特になし"
    else:
        side_effects_summary = "特になし"
    def fmt_score(obj): return f"HAMD17 {obj.total_score_17}点 HAMD21 {obj.total_score_21}点" if obj else "未評価"
    # Prefer using the service-built history_list so counts match the displayed table
    if history_list and len(history_list) > 0:
        first_date = history_list[0].get('date')
        last_date = history_list[-1].get('date')
        try:
            start_date_str = first_date.strftime('%Y年%m月%d日') if first_date else "未開始"
        except Exception:
            start_date_str = str(first_date) if first_date else "未開始"
        try:
            end_date_str = patient.discharge_date.strftime('%Y年%m月%d日') if patient.discharge_date else (last_date.strftime('%Y年%m月%d日') if last_date else "未定")
        except Exception:
            end_date_str = str(last_date) if last_date else (patient.discharge_date.strftime('%Y年%m月%d日') if patient.discharge_date else "未定")
        total_count = len(history_list)
    else:
        start_date_str = "未開始"
        end_date_str = patient.discharge_date.strftime('%Y年%m月%d日') if patient.discharge_date else "未定"
        total_count = 0
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

    survey_sessions = PatientSurveySession.objects.filter(
        patient=patient,
        course_number=course_number,
    ).prefetch_related("responses").order_by("-started_at")
    survey_summary = []
    for s in survey_sessions:
        resp_map = {r.instrument: r for r in s.responses.all()}
        survey_summary.append({
            "session": s,
            "totals": {code: (resp_map.get(code).total_score if resp_map.get(code) else None) for code in INSTRUMENT_ORDER},
            "phq9_q10": resp_map.get("phq9").phq9_difficulty if resp_map.get("phq9") else None,
        })
    survey_by_phase = {}
    for row in survey_summary:
        phase = row['session'].phase
        if phase not in survey_by_phase:
            survey_by_phase[phase] = row
    survey_phase_summary = [
        {
            'phase': phase,
            'label': '治療前' if phase == 'pre' else '治療後',
            'row': survey_by_phase.get(phase),
        }
        for phase in ('pre', 'post')
    ]
    return render(request, 'rtms_app/patient_summary.html', {
        'patient': patient,
        'summary_text': summary_text,
        'history_list': history_list,
        'today': timezone.now().date(),
        'test_scores': test_scores,
        'trend_cols': trend_cols,
        'hamd_trend_cols': trend_cols,
        'dashboard_date': dashboard_date,
        'floating_print_options': floating_print_options,
        'can_view_audit': can_view_audit(request.user),
        'survey_summary': survey_summary,
        'survey_phase_summary': survey_phase_summary,
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
            new_patient = Patient(card_id=latest.card_id, name=latest.name, birth_date=latest.birth_date, gender=latest.gender, referral_source=request.POST.get('referral_source') or latest.referral_source, referral_doctor=request.POST.get('referral_doctor') or latest.referral_doctor, life_history=latest.life_history, past_history=latest.past_history, diagnosis=latest.diagnosis, first_visit_date=parse_date(request.POST.get('first_visit_date')) or timezone.localdate(), course_number=new_course_num)
            new_patient.save()
            return redirect('rtms_app:dashboard')
        if existing_patients.exists():
            latest = existing_patients.first()
            return render(request, 'rtms_app/patient_add.html', {'form': form, 'referral_options': referral_options, 'existing_patient': latest, 'next_course_num': latest.course_number + 1})
        if form.is_valid():
            register_patient_with_initial_course(form.save(commit=False))
            return redirect('rtms_app:dashboard')
    else: form = PatientRegistrationForm()
    return render(request, 'rtms_app/patient_add.html', {'form': form, 'referral_options': referral_options})


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
    try:
        course_number = int(request.GET.get('course_number', patient.course_number or 1))
    except (TypeError, ValueError):
        course_number = patient.course_number or 1
    treatment_course = TreatmentCourse.objects.filter(
        patient=patient, course_number=course_number,
    ).first()
    course_admission_date = (
        treatment_course.admission_date
        if treatment_course and treatment_course.admission_date
        else patient.admission_date
    )
    # ★修正: generate_calendar_weeks を使用
    calendar_weeks, assessment_events = generate_calendar_weeks(
        patient, treatment_course=treatment_course, course_number=course_number,
    )
    last_assessment = get_latest_assessment(
        patient, 'week3', treatment_course=treatment_course, course_number=course_number,
    )
    baseline_assessment = get_latest_assessment(
        patient, 'baseline', treatment_course=treatment_course, course_number=course_number,
    )
    week6_assessment = get_latest_assessment(
        patient, 'week6', treatment_course=treatment_course, course_number=course_number,
    )
    return render(request, 'rtms_app/patient_clinical_path.html', {
        'patient': patient,
        'treatment_course': treatment_course,
        'course_number': course_number,
        'course_admission_date': course_admission_date,
        'calendar_weeks': calendar_weeks,
        'assessment_events': assessment_events,
        'treatment_overflow': get_treatment_overflow_info(patient),
        'last_assessment': last_assessment,
        'baseline_assessment': baseline_assessment,
        'week6_assessment': week6_assessment,
        'today': timezone.now().date(),
        'dashboard_date': dashboard_date,
        'can_view_audit': can_view_audit(request.user)
    })


@login_required
def clinical_path_reschedule(request, patient_id):
    """クリニカルパス画面のドラッグ&ドロップによる予定変更を受け付けるAPI。

    event_type: 'admission' | 'discharge' | 'treatment' | 'mapping' | 'assessment'
    treatment / mapping の場合はさらに status: 'planned' | 'done' と session_id, source_date が必要。
    mapping の場合、status == 'planned' のときは week_number も必要（週単位で個別に移動。他週への連動なし）。
    assessment の場合、scale_code, timing が必要（尺度・時期単位で個別に移動。他タイミングへの連動なし。
    実施済みの評価は移動不可）。
    """
    patient = get_object_or_404(Patient, pk=patient_id)
    if request.method != 'POST':
        return JsonResponse({'error': 'POSTメソッドが必要です'}, status=405)

    try:
        payload = json.loads(request.body.decode('utf-8') or '{}')
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'error': 'リクエスト内容が不正です'}, status=400)

    event_type = payload.get('event_type')
    if event_type not in {'admission', 'discharge', 'treatment', 'mapping', 'assessment'}:
        return JsonResponse({'error': 'イベント種別が不正です'}, status=400)

    target_date = parse_date(payload.get('target_date') or '')
    if not target_date:
        return JsonResponse({'error': '移動先の日付が不正です'}, status=400)

    if event_type == 'admission':
        try:
            course_number = int(payload.get('course_number', patient.course_number or 1))
        except (TypeError, ValueError):
            return JsonResponse({'error': 'クール番号が不正です'}, status=400)
        treatment_course = TreatmentCourse.objects.filter(
            patient=patient, course_number=course_number,
        ).first()
        if treatment_course is not None:
            treatment_course.admission_date = target_date
            treatment_course.save(update_fields=['admission_date'])
        if treatment_course is None or treatment_course.course_number == 1:
            patient.admission_date = target_date
            patient.save(update_fields=['admission_date'])
        return JsonResponse({'status': 'ok'})

    if event_type == 'discharge':
        patient.discharge_date = target_date
        patient.save(update_fields=['discharge_date'])
        return JsonResponse({'status': 'ok'})

    if event_type == 'treatment':
        source_date = parse_date(payload.get('source_date') or '')
        if not source_date:
            return JsonResponse({'error': '移動元の日付が不正です'}, status=400)

        status = payload.get('status')
        session_id = payload.get('session_id')
        try:
            course_number = int(payload.get('course_number', patient.course_number or 1))
        except (TypeError, ValueError):
            return JsonResponse({'error': 'クール番号が不正です'}, status=400)
        treatment_course = TreatmentCourse.objects.filter(
            patient=patient, course_number=course_number,
        ).first()
        session_scope = {'treatment_course': treatment_course} if treatment_course else {
            'patient': patient, 'course_number': course_number,
        }
        course_first_treatment_date = getattr(treatment_course, 'first_treatment_date', None) or patient.first_treatment_date
        course_discharge_date = getattr(treatment_course, 'discharge_date', None) or patient.discharge_date

        if status == 'done':
            session = TreatmentSession.objects.filter(
                **session_scope, pk=session_id, status='done',
            ).first()
            if not session:
                return JsonResponse({'error': '対象の実施記録が見つかりません'}, status=404)
            if session.session_date != source_date:
                return JsonResponse({'error': '移動元の日付と実施記録が一致しません'}, status=400)
            conflict = TreatmentSession.objects.filter(
                **session_scope, session_date=target_date,
            ).exclude(pk=session.pk).exists()
            if conflict:
                return JsonResponse({'error': 'その日には既に予定・実施記録があります'}, status=400)
            delta = target_date - session.session_date
            if session.date:
                session.date = session.date + delta
            session.session_date = target_date
            if treatment_course is not None:
                save_treatment_session_strict(
                    session, patient, treatment_course, course_number=course_number,
                )
            else:
                save_treatment_session_legacy(session, patient, course_number)
            return JsonResponse({'status': 'ok'})

        if status not in (None, 'planned'):
            return JsonResponse({'error': '治療予定の状態が不正です'}, status=400)

        # planned（または未指定＝まだDB行がないcanonical予定）
        if not course_first_treatment_date:
            return JsonResponse({'error': '初回治療日が未設定です'}, status=400)

        treat_dates = generate_treatment_dates(course_first_treatment_date, total=30, holidays=JP_HOLIDAYS)
        if course_discharge_date:
            treat_dates = [d for d in treat_dates if d < course_discharge_date]

        # The browser sends the concrete TreatmentSession id for materialized
        # events.  Prefer it over the date: a date can still be a canonical
        # (virtual) slot after a previous manual reschedule.
        existing_session = None
        if session_id not in (None, ''):
            try:
                session_pk = int(session_id)
            except (TypeError, ValueError):
                return JsonResponse({'error': '治療記録IDが不正です'}, status=400)
            existing_session = TreatmentSession.objects.filter(
                **session_scope, pk=session_pk,
            ).first()
            if not existing_session:
                return JsonResponse({'error': '移動対象の治療予定が見つかりません'}, status=404)
            if existing_session.session_date != source_date:
                return JsonResponse({'error': '移動元の日付と治療予定が一致しません'}, status=400)
        else:
            existing_session = TreatmentSession.objects.filter(
                **session_scope,
                session_date=source_date,
            ).first()
        if existing_session and existing_session.status != 'planned':
            return JsonResponse({'error': '実施済みまたはスキップ済みの治療は移動できません'}, status=400)
        if not existing_session and source_date not in treat_dates:
            return JsonResponse({'error': '移動元の日付に予定された治療がありません'}, status=404)

        # Capture whether the dragged event is the current first materialized
        # treatment before any canonical rows are materialized.  The session id
        # is authoritative; the date fallback is only for virtual day 1.
        first_session = get_treatment_sessions(patient, course_number=course_number)[:1]
        is_first_treatment = bool(
            (existing_session and first_session and existing_session.pk == first_session[0].pk)
            or (existing_session is None and source_date == course_first_treatment_date)
        )

        exceptional_treatment_day = not is_treatment_day(target_date)
        if target_date == source_date:
            return JsonResponse({'error': '移動先は現在の日付と異なる治療日を指定してください'}, status=400)

        conflict = TreatmentSession.objects.filter(
            **session_scope,
            session_date=target_date,
            status__in=['done', 'skipped'],
        ).exists()
        if conflict:
            return JsonResponse({'error': '移動先には実施済みまたはスキップ済みの治療があります'}, status=400)

        try:
            with transaction.atomic():
                if existing_session is None:
                    Patient.objects.select_for_update().get(pk=patient.pk)
                    if not can_create_treatment_session(patient, course_number):
                        raise ValueError('この患者・コースは治療予定が30回以上あるため、新規予定を追加できません')
                    existing_dates = set(
                        TreatmentSession.objects.filter(
                            **session_scope,
                        ).values_list('session_date', flat=True)
                    )
                    # Materialize the complete canonical sequence on the first
                    # manual move.  This makes TreatmentSession the authoritative
                    # source for subsequent numbering and prevents the vacated
                    # canonical date from producing a duplicate virtual event.
                    capacity = MAX_TREATMENT_SESSIONS - len(get_treatment_sessions(patient, course_number))
                    candidate_dates = [d for d in treat_dates if d not in existing_dates]
                    if source_date in candidate_dates:
                        candidate_dates.remove(source_date)
                        candidate_dates.insert(0, source_date)
                    to_create = candidate_dates[:capacity]
                    if source_date not in to_create:
                        raise ValueError('移動対象の予定を30回枠内に作成できません')
                    materialized_sessions = [
                        TreatmentSession(
                            patient=patient,
                            treatment_course=treatment_course,
                            course_number=course_number,
                            session_date=d,
                            date=timezone.make_aware(datetime.datetime.combine(d, datetime.time(hour=9))),
                            status='planned',
                        )
                        for d in to_create
                    ]
                    if treatment_course is not None:
                        bulk_create_treatment_sessions_strict(
                            patient, treatment_course, materialized_sessions,
                        )
                    else:
                        bulk_create_treatment_sessions_legacy(
                            patient, course_number, materialized_sessions,
                        )
                    existing_session = TreatmentSession.objects.filter(
                        **session_scope,
                        session_date=source_date,
                        status='planned',
                    ).first()
                    if existing_session is None:
                        raise ValueError('移動対象の予定を作成できませんでした')

                if is_first_treatment:
                    reschedule_treatment_start_date(
                        patient,
                        target_date,
                        course_number=course_number,
                        holidays=JP_HOLIDAYS,
                        allow_exceptional_day=exceptional_treatment_day,
                    )
                else:
                    reschedule_planned_session(
                        patient,
                        existing_session,
                        target_date,
                        allow_exceptional_day=exceptional_treatment_day,
                    )
        except (IntegrityError, ValueError) as e:
            if isinstance(e, IntegrityError):
                return JsonResponse({'error': '治療予定を安全に変更できませんでした'}, status=400)
            return JsonResponse({'error': str(e)}, status=400)

        return JsonResponse({'status': 'ok'})

    if event_type == 'mapping':
        source_date = parse_date(payload.get('source_date') or '')
        if not source_date:
            return JsonResponse({'error': '移動元の日付が不正です'}, status=400)
        if not is_treatment_day(target_date):
            return JsonResponse({'error': '土日祝日にはMT測定を予定できません'}, status=400)

        status = payload.get('status')
        try:
            course_number = int(payload.get('course_number', patient.course_number or 1))
        except (TypeError, ValueError):
            return JsonResponse({'error': 'クール番号が不正です'}, status=400)
        treatment_course = TreatmentCourse.objects.filter(
            patient=patient, course_number=course_number,
        ).first()
        mapping_scope = {'treatment_course': treatment_course} if treatment_course else {
            'patient': patient, 'course_number': course_number,
        }

        if status == 'done':
            session_id = payload.get('session_id')
            mapping_session = MappingSession.objects.filter(
                **mapping_scope, pk=session_id,
            ).first()
            if not mapping_session:
                return JsonResponse({'error': '対象のMT測定記録が見つかりません'}, status=404)
            if mapping_session.date != source_date:
                return JsonResponse({'error': '移動元の日付とMT測定記録が一致しません'}, status=400)
            conflict = MappingSession.objects.filter(
                **mapping_scope, date=target_date,
                stimulation_site=mapping_session.stimulation_site,
            ).exclude(pk=mapping_session.pk).exists()
            if conflict:
                return JsonResponse({'error': 'その日には既にMT測定記録があります'}, status=400)
            mapping_session.date = target_date
            if treatment_course is not None:
                save_mapping_session_strict(
                    mapping_session, patient, treatment_course, course_number=course_number,
                )
            else:
                save_mapping_session_legacy(mapping_session, patient, course_number)
            return JsonResponse({'status': 'ok'})

        if status not in (None, 'planned'):
            return JsonResponse({'error': 'MT測定予定の状態が不正です'}, status=400)

        # planned：その週の予定だけを個別に移動する（他週への連動・カスケードなし）
        try:
            week_number = int(payload.get('week_number'))
        except (TypeError, ValueError):
            return JsonResponse({'error': '対象の週が不明です'}, status=400)
        if week_number not in dict(MappingSession.WEEK_CHOICES):
            return JsonResponse({'error': '対象の週が不正です'}, status=400)

        mapping_base = patient.mapping_date or patient.first_treatment_date
        if mapping_base is None:
            return JsonResponse({'error': 'MT測定の基準日が未設定です'}, status=400)
        override = MappingSchedule.objects.filter(
            **mapping_scope, week_number=week_number,
        ).first()
        if override is not None:
            current_planned_date = override.planned_date
        else:
            generated = generate_mapping_dates(mapping_base, weeks=8, holidays=JP_HOLIDAYS)
            current_planned_date = next(
                (item['actual'] for item in generated if item['week_no'] == week_number),
                None,
            )
        if current_planned_date != source_date:
            return JsonResponse({'error': '移動元の日付に該当するMT測定予定がありません'}, status=404)

        schedule_conflict = MappingSchedule.objects.filter(
            **mapping_scope,
            planned_date=target_date,
        ).exclude(week_number=week_number).exists()
        actual_conflict = MappingSession.objects.filter(
            **mapping_scope,
            date=target_date,
        ).exists()
        if schedule_conflict or actual_conflict:
            return JsonResponse({'error': '移動先には別のMT測定予定または実績があります'}, status=400)

        with transaction.atomic():
            if treatment_course is not None:
                update_or_create_mapping_schedule_strict(
                    patient,
                    treatment_course,
                    week_number=week_number,
                    planned_date=target_date,
                    course_number=course_number,
                )
            else:
                update_or_create_mapping_schedule_legacy(
                    patient,
                    course_number,
                    week_number=week_number,
                    planned_date=target_date,
                )
        return JsonResponse({'status': 'ok'})

    if event_type == 'assessment':
        if not is_treatment_day(target_date):
            return JsonResponse({'error': '土日祝日には評価を予定できません'}, status=400)

        scale_code = payload.get('scale_code')
        timing = payload.get('timing')
        if not scale_code or not timing:
            return JsonResponse({'error': '対象の尺度・時期が不明です'}, status=400)
        if timing not in dict(Assessment.TIMING_CHOICES):
            return JsonResponse({'error': '評価時期が不正です'}, status=400)

        try:
            course_number = int(payload.get('course_number', patient.course_number or 1))
        except (TypeError, ValueError):
            return JsonResponse({'error': 'クール番号が不正です'}, status=400)
        treatment_course = resolve_treatment_course(patient, course_number=course_number)
        assessment_scope = {'treatment_course': treatment_course} if treatment_course else {
            'patient': patient, 'course_number': course_number,
        }

        if scale_code == OTHER_SCALES_SCHEDULE_CODE:
            # HAM-D以外の尺度は一括で実施するため、対象タイミングの全尺度の予定日を連動して更新する
            scales = list(ScaleDefinition.objects.filter(is_active=True).exclude(code='hamd'))
            if not scales:
                return JsonResponse({'error': '対象の尺度が見つかりません'}, status=404)
            is_done = all(
                AssessmentRecord.objects.filter(
                    **assessment_scope, timing=timing, scale=scale,
                ).exists()
                for scale in scales
            )
            if is_done:
                return JsonResponse({'error': '実施済みの評価は移動できません'}, status=400)
            with transaction.atomic():
                for scale in scales:
                    if treatment_course is not None:
                        update_or_create_assessment_schedule_strict(
                            patient,
                            treatment_course,
                            scale=scale,
                            timing=timing,
                            planned_date=target_date,
                            course_number=course_number,
                        )
                    else:
                        update_or_create_assessment_schedule_legacy(
                            patient,
                            course_number,
                            scale=scale,
                            timing=timing,
                            planned_date=target_date,
                        )
            return JsonResponse({'status': 'ok'})

        scale = ScaleDefinition.objects.filter(code=scale_code).first()
        if not scale:
            return JsonResponse({'error': '対象の尺度が見つかりません'}, status=404)

        is_done = AssessmentRecord.objects.filter(
            **assessment_scope, timing=timing, scale=scale,
        ).exists()
        if scale.code == 'hamd':
            is_done = is_done or Assessment.objects.filter(
                **assessment_scope,
                timing=timing, type='HAM-D',
            ).exists()
        if is_done:
            return JsonResponse({'error': '実施済みの評価は移動できません'}, status=400)

        with transaction.atomic():
            if treatment_course is not None:
                update_or_create_assessment_schedule_strict(
                    patient,
                    treatment_course,
                    scale=scale,
                    timing=timing,
                    planned_date=target_date,
                    course_number=course_number,
                )
            else:
                update_or_create_assessment_schedule_legacy(
                    patient,
                    course_number,
                    scale=scale,
                    timing=timing,
                    planned_date=target_date,
                )
        return JsonResponse({'status': 'ok'})

    return JsonResponse({'error': '不明なイベント種別です'}, status=400)

@login_required
def patient_print_path(request, patient_id):
    patient = get_object_or_404(Patient, pk=patient_id)
    course_number = request.GET.get('course_number')
    treatment_course = resolve_treatment_course(patient, course_number=course_number)
    calendar_weeks, assessment_events = generate_calendar_weeks(patient, treatment_course=treatment_course)
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
    dashboard_date = get_dashboard_date(request)
    
    context = build_common_context(patient, dashboard_date, logs=logs)
    return render(request, 'rtms_app/audit_logs.html', context)

@login_required
def latest_consent(request):
    doc = ConsentDocument.objects.order_by("-uploaded_at").first()
    if doc and doc.file:
        return redirect(doc.file.url)
    # アップロードが無い / 初期化で消えた → 静的ファイルへフォールバック
    return redirect(static("rtms_app/docs/consent_default.pdf"))
