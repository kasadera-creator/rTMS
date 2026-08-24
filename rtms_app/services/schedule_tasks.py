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

from ..models import Patient, MappingSession, Assessment, AssessmentRecord, MappingSchedule, TreatmentSession
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


def _assessment_performed_date(patient: Patient, timing: str) -> Optional[datetime.date]:
    """Return the performed/recorded date for an assessment timing if it exists.

    Preference order:
    - `Assessment.performed_date` if present on model
    - `Assessment.date` (legacy)
    - None
    """
    course_number = patient.course_number or 1
    a = Assessment.objects.filter(
        patient=patient, course_number=course_number, timing=timing
    ).order_by('date').first()
    if a:
        # prefer performed_date if available
        if hasattr(a, 'performed_date') and getattr(a, 'performed_date'):
            return a.performed_date
        if a.date:
            return a.date
    record = AssessmentRecord.objects.filter(
        patient=patient, course_number=course_number, timing=timing,
        scale__code='hamd',
    ).order_by('date').first()
    return record.date if record else None


def _mapping_performed_date_for_nominal(patient: Patient, nominal_date: datetime.date) -> Optional[datetime.date]:
    """Return MappingSession.date if a mapping session exists for the nominal/actual date.

    MappingSession stores performed mapping `date` already; callers may match by actual date.
    """
    ms = MappingSession.objects.filter(
        patient=patient, course_number=patient.course_number or 1, date=nominal_date
    ).order_by('date').first()
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


def compute_task_definitions(patient: Patient, holidays: Optional[set] = None) -> List[Dict]:
    """Return task definitions for mapping and assessments.

    Each task dict contains:
      - key: internal key (e.g., 'mapping_week2', 'assessment_week3')
      - label: human label
      - planned_date: date
      - window_start/window_end: allowed window for completion (dates)
      - performed_date: if already performed (may be None)
    """
    tasks: List[Dict] = []
    today = timezone.localdate()
    day1 = get_treatment_day1(patient)

    # Mapping: nominal next-week same weekday (k=1 in generate_mapping_dates)
    if day1:
        mapping_list = generate_mapping_dates(day1, weeks=8, holidays=holidays)
        # prefer week index 1 (the "next week") as mapping appointment
        if len(mapping_list) > 1:
            mapping_task = mapping_list[1]
            planned = MappingSchedule.objects.filter(
                patient=patient, course_number=patient.course_number or 1,
                week_number=mapping_task['week_no'],
            ).values_list('planned_date', flat=True).first() or mapping_task['actual']
            perf = _mapping_performed_date_for_nominal(patient, planned)
            _, deadline = _treatment_week_window(day1, mapping_task['week_no'], holidays)
            tasks.append({
                'key': 'mapping',
                'label': 'MT測定',
                'planned_date': planned,
                'window_start': planned,
                'window_end': deadline or planned,
                'performed_date': perf,
            })

    # Assessments: baseline / week3 / week4 (all-case only) / week6
    # Baseline: default to patient.created_at date if available, else today
    baseline_planned = getattr(patient, 'first_visit_date', None)
    if not baseline_planned:
        created_at = getattr(patient, 'created_at', None)
        baseline_planned = created_at.date() if created_at else today
    if day1 and baseline_planned > day1:
        baseline_planned = day1
    baseline_perf = _assessment_performed_date(patient, 'baseline')
    tasks.append({
        'key': 'assessment_baseline',
        'label': '治療前評価',
        'planned_date': baseline_planned,
        'window_start': baseline_planned,
        'window_end': baseline_planned,
        'performed_date': baseline_perf,
    })

    if day1:
        w3_planned, w3_end = _treatment_week_window(day1, 3, holidays)
        w3_perf = _assessment_performed_date(patient, 'week3')
        tasks.append({
            'key': 'assessment_week3',
            'label': '第3週目評価',
            'planned_date': w3_planned,
            'window_start': w3_planned,
            'window_end': w3_end,
            'performed_date': w3_perf,
        })

        # Week4 (4週経過後): only for all-case-survey patients
        if getattr(patient, 'is_all_case_survey', False):
            _, w4_planned = _treatment_week_window(day1, 4, holidays)
            w4_window_end = w4_planned + datetime.timedelta(days=7) if w4_planned else None
            w4_perf = _assessment_performed_date(patient, 'week4')
            tasks.append({
                'key': 'assessment_week4',
                'label': '4週経過後HAM-D評価',
                'planned_date': w4_planned,
                'window_start': w4_planned,
                'window_end': w4_window_end,
                'performed_date': w4_perf,
            })

        # Week6 uses the first and last canonical treatment dates in week 6.
        w6_planned, w6_end = _treatment_week_window(day1, 6, holidays)
        w6_perf = _assessment_performed_date(patient, 'week6')
        tasks.append({
            'key': 'assessment_week6',
            'label': '第6週目評価',
            'planned_date': w6_planned,
            'window_start': w6_planned,
            'window_end': w6_end,
            'performed_date': w6_perf,
        })

    return tasks


def compute_dashboard_tasks(patient: Patient, today: Optional[datetime.date] = None, holidays: Optional[set] = None) -> List[Dict]:
    """Compute tasks that should appear on the dashboard for `patient` as of `today`.

    Rule: include tasks where `planned_date <= today` and `performed_date` is None.
    Returns a list of task dicts (subset of compute_task_definitions entries).
    """
    today = today or timezone.localdate()
    defs = compute_task_definitions(patient, holidays=holidays)
    todo = []
    for t in defs:
        pd = t.get('planned_date')
        perf = t.get('performed_date')
        window_start = t.get('window_start') or pd
        window_end = t.get('window_end') or pd
        if pd and window_start <= today <= window_end and not perf:
            todo.append(t)
    return todo
