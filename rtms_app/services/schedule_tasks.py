"""Schedule task generation and helpers.

This module wraps lower-level `rtms_schedule` utilities and exposes
task-oriented APIs for clinical-path and dashboard consumption.

Functions provided here are intentionally conservative: they do not
modify DB state. Higher-level callers (views / admin actions) should
invoke the mutation helpers (postponement / cancellation) after
recording `TreatmentSkip` entries and any necessary audits.
"""
from __future__ import annotations

import datetime
from typing import List, Optional, Dict

from django.utils import timezone

from ..models import Patient, TreatmentCourse, MappingSession, Assessment, AssessmentRecord, MappingSchedule, TreatmentSession
from ..queries.assessment_queries import resolve_treatment_course
from .rtms_schedule import (
    is_closed,
    next_open_day,
    generate_mapping_dates,
    generate_treatment_dates,
)


def is_business_day(d: datetime.date, holidays: Optional[set] = None) -> bool:
    """Return True if clinic is open on date `d` (Mon-Fri, not holidays/year-end)."""
    return not is_closed(d, holidays)


def next_business_day(d: datetime.date, holidays: Optional[set] = None) -> datetime.date:
    """Roll forward to the next business day (inclusive of d)."""
    return next_open_day(d, holidays)


def shift_to_next_business_day_if_needed(d: datetime.date, holidays: Optional[set] = None) -> datetime.date:
    """If `d` is closed, return the next open day; otherwise return `d`."""
    return d if is_business_day(d, holidays) else next_open_day(d, holidays)


def get_treatment_day1(patient: Patient) -> Optional[datetime.date]:
    """Return canonical day1 (first_treatment_date) for patient or None."""
    return getattr(patient, 'first_treatment_date', None)


def _assessment_performed_date(patient: Patient, timing: str, treatment_course=None) -> Optional[datetime.date]:
    """Return the performed/recorded date for an assessment timing if it exists.

    Preference order:
    - `Assessment.performed_date` if present on model
    - `Assessment.date` (legacy)
    - None
    """
    treatment_course = resolve_treatment_course(patient, treatment_course=treatment_course)
    scope = {'treatment_course': treatment_course} if treatment_course else {
        'patient': patient, 'course_number': patient.course_number or 1,
    }
    a = Assessment.objects.filter(
        **scope, timing=timing
    ).order_by('date').first()
    if a:
        # prefer performed_date if available
        if hasattr(a, 'performed_date') and getattr(a, 'performed_date'):
            return a.performed_date
        if a.date:
            return a.date
    record = AssessmentRecord.objects.filter(
        **scope, timing=timing,
        scale__code='hamd',
    ).order_by('date').first()
    return record.date if record else None


def _mapping_performed_date_for_nominal(patient: Patient, nominal_date: datetime.date, treatment_course=None) -> Optional[datetime.date]:
    """Return MappingSession.date if a mapping session exists for the nominal/actual date.

    MappingSession stores performed mapping `date` already; callers may match by actual date.
    """
    treatment_course = treatment_course or TreatmentCourse.objects.filter(
        patient=patient, course_number=patient.course_number or 1,
    ).first()
    scope = {'treatment_course': treatment_course} if treatment_course else {
        'patient': patient, 'course_number': patient.course_number or 1,
    }
    ms = MappingSession.objects.filter(**scope, date=nominal_date).order_by('date').first()
    return ms.date if ms else None


def _treatment_week_window(day1: Optional[datetime.date], week_number: int,
                           holidays: Optional[set]) -> tuple[Optional[datetime.date], Optional[datetime.date]]:
    """Return the first/last canonical treatment date in a 5-session week."""
    if not day1:
        return None, None
    dates = generate_treatment_dates(day1, total=30, holidays=holidays)
    start = (week_number - 1) * 5
    block = dates[start:start + 5]
    return (block[0], block[-1]) if block else (None, None)


def _resolve_task_context(patient: Patient, treatment_course=None) -> Dict:
    """Resolve the Course and dates shared by task generators."""
    treatment_course = resolve_treatment_course(patient, treatment_course=treatment_course)
    course_number = getattr(treatment_course, 'course_number', None) or patient.course_number or 1
    day1 = getattr(treatment_course, 'first_treatment_date', None) or get_treatment_day1(patient)
    mapping_base = getattr(treatment_course, 'mapping_date', None) or day1
    scope = {'treatment_course': treatment_course} if treatment_course else {
        'patient': patient, 'course_number': course_number,
    }
    return {
        'treatment_course': treatment_course,
        'course_number': course_number,
        'day1': day1,
        'mapping_base': mapping_base,
        'scope': scope,
    }


def _compute_mapping_tasks(patient: Patient, context: Dict, holidays: Optional[set]) -> List[Dict]:
    """Build mapping tasks using the resolved Course context."""
    day1 = context['day1']
    if not day1 or not context['mapping_base']:
        return []

    mapping_list = generate_mapping_dates(context['mapping_base'], weeks=8, holidays=holidays)
    if len(mapping_list) <= 1:
        return []

    mapping_task = mapping_list[1]
    planned = MappingSchedule.objects.filter(
        **context['scope'],
        week_number=mapping_task['week_no'],
    ).values_list('planned_date', flat=True).first() or mapping_task['actual']
    perf = _mapping_performed_date_for_nominal(
        patient, planned, context['treatment_course'],
    )
    _, deadline = _treatment_week_window(day1, mapping_task['week_no'], holidays)
    return [{
        'key': 'mapping',
        'label': 'MT測定',
        'planned_date': planned,
        'window_start': planned,
        'window_end': deadline or planned,
        'performed_date': perf,
    }]


def _compute_hamd_week_task(
    patient: Patient,
    context: Dict,
    week_number: int,
    holidays: Optional[set],
) -> Optional[Dict]:
    """Build one scheduled HAM-D task for a treatment week."""
    day1 = context['day1']
    week_start, week_end = _treatment_week_window(day1, week_number, holidays)
    planned = week_end if week_number == 4 else week_start
    if not planned:
        return None
    if week_number == 4:
        window_end = planned + datetime.timedelta(days=7)
    else:
        window_end = week_end
    return {
        'key': f'assessment_week{week_number}',
        'label': f'{week_number}週経過後HAM-D評価' if week_number == 4 else f'第{week_number}週目評価',
        'planned_date': planned,
        'window_start': planned,
        'window_end': window_end,
        'performed_date': _assessment_performed_date(
            patient, f'week{week_number}', context['treatment_course'],
        ),
    }


def _compute_assessment_tasks(patient: Patient, context: Dict, holidays: Optional[set]) -> List[Dict]:
    """Build baseline and treatment-week Assessment/HAM-D tasks."""
    today = timezone.localdate()
    day1 = context['day1']
    treatment_course = context['treatment_course']
    tasks: List[Dict] = []

    # Assessments: baseline / week3 / week4 (all-case only) / week6
    # Baseline: default to patient.created_at date if available, else today
    baseline_planned = getattr(patient, 'first_visit_date', None)
    if not baseline_planned:
        created_at = getattr(patient, 'created_at', None)
        baseline_planned = created_at.date() if created_at else today
    if day1 and baseline_planned > day1:
        baseline_planned = day1
    baseline_perf = _assessment_performed_date(patient, 'baseline', treatment_course)
    tasks.append({
        'key': 'assessment_baseline',
        'label': '治療前評価',
        'planned_date': baseline_planned,
        'window_start': baseline_planned,
        'window_end': baseline_planned,
        'performed_date': baseline_perf,
    })

    if day1:
        week3_task = _compute_hamd_week_task(patient, context, 3, holidays)
        if week3_task:
            tasks.append(week3_task)

        # Week4 (4週経過後): only for all-case-survey patients
        if getattr(patient, 'is_all_case_survey', False):
            week4_task = _compute_hamd_week_task(patient, context, 4, holidays)
            if week4_task:
                tasks.append(week4_task)

        # Week6 uses the first and last canonical treatment dates in week 6.
        week6_task = _compute_hamd_week_task(patient, context, 6, holidays)
        if week6_task:
            tasks.append(week6_task)

    return tasks


def compute_task_definitions(patient: Patient, holidays: Optional[set] = None, treatment_course=None) -> List[Dict]:
    """Return task definitions for mapping and assessments.

    Each task dict contains:
      - key: internal key (e.g., 'mapping_week2', 'assessment_week3')
      - label: human label
      - planned_date: date
      - window_start/window_end: allowed window for completion (dates)
      - performed_date: if already performed (may be None)
    """
    context = _resolve_task_context(patient, treatment_course)
    return (
        _compute_mapping_tasks(patient, context, holidays)
        + _compute_assessment_tasks(patient, context, holidays)
    )


def compute_dashboard_tasks(patient: Patient, today: Optional[datetime.date] = None, holidays: Optional[set] = None, treatment_course=None) -> List[Dict]:
    """Compute tasks that should appear on the dashboard for `patient` as of `today`.

    Rule: include tasks where `planned_date <= today` and `performed_date` is None.
    Returns a list of task dicts (subset of compute_task_definitions entries).
    """
    today = today or timezone.localdate()
    defs = compute_task_definitions(patient, holidays=holidays, treatment_course=treatment_course)
    todo = []
    for t in defs:
        pd = t.get('planned_date')
        perf = t.get('performed_date')
        window_start = t.get('window_start') or pd
        window_end = t.get('window_end') or pd
        if pd and window_start <= today <= window_end and not perf:
            todo.append(t)
    return todo
