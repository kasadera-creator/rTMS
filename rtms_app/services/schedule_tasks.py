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

from ..models import Patient, MappingSession, Assessment, TreatmentSession
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
    a = Assessment.objects.filter(patient=patient, timing=timing).order_by('date').first()
    if not a:
        return None
    # prefer performed_date if available
    if hasattr(a, 'performed_date') and getattr(a, 'performed_date'):
        return a.performed_date
    return a.date


def _mapping_performed_date_for_nominal(patient: Patient, nominal_date: datetime.date) -> Optional[datetime.date]:
    """Return MappingSession.date if a mapping session exists for the nominal/actual date.

    MappingSession stores performed mapping `date` already; callers may match by actual date.
    """
    ms = MappingSession.objects.filter(patient=patient, date=nominal_date).order_by('date').first()
    return ms.date if ms else None


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
            planned = mapping_task['actual']
            perf = _mapping_performed_date_for_nominal(patient, planned)
            tasks.append({
                'key': 'mapping',
                'label': '位置決め',
                'planned_date': planned,
                'window_start': planned,
                'window_end': planned,
                'performed_date': perf,
            })

    # Assessments: baseline / week3 / week4 (all-case only) / week6
    # Baseline: default to patient.created_at date if available, else today
    baseline_planned = getattr(patient, 'created_at', None)
    baseline_planned = baseline_planned.date() if baseline_planned else today
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
        # Week3: planned = day1 + 14 (day15), window = day15..day21 (adjust business days)
        w3_nominal_start = day1 + datetime.timedelta(days=14)
        w3_nominal_end = day1 + datetime.timedelta(days=20)
        w3_planned = shift_to_next_business_day_if_needed(w3_nominal_start, holidays)
        # compute last business day <= nominal_end
        w3_end = w3_nominal_end
        while not is_business_day(w3_end, holidays) and w3_end > w3_nominal_start:
            w3_end -= datetime.timedelta(days=1)
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
            # planned = last business day of week4 (day1 + 27)
            w4_nominal_last = day1 + datetime.timedelta(days=27)
            w4_planned = w4_nominal_last
            # roll back to last business day if needed
            while not is_business_day(w4_planned, holidays) and w4_planned > day1:
                w4_planned -= datetime.timedelta(days=1)
            # window_end = last business day on or before planned + 7 days
            w4_window_end_candidate = w4_planned + datetime.timedelta(days=7)
            # move backward to last business day <= candidate
            w4_window_end = w4_window_end_candidate
            while not is_business_day(w4_window_end, holidays) and w4_window_end > w4_planned:
                w4_window_end -= datetime.timedelta(days=1)
            w4_perf = _assessment_performed_date(patient, 'week4')
            tasks.append({
                'key': 'assessment_week4',
                'label': '4週経過後',
                'planned_date': w4_planned,
                'window_start': w4_planned,
                'window_end': w4_window_end,
                'performed_date': w4_perf,
            })

        # Week6: planned = day1 + 35 (day36), window = day36..day42
        w6_nominal_start = day1 + datetime.timedelta(days=35)
        w6_nominal_end = day1 + datetime.timedelta(days=41)
        w6_planned = shift_to_next_business_day_if_needed(w6_nominal_start, holidays)
        w6_end = w6_nominal_end
        while not is_business_day(w6_end, holidays) and w6_end > w6_nominal_start:
            w6_end -= datetime.timedelta(days=1)
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
        if pd and pd <= today and not perf:
            todo.append(t)
    return todo
