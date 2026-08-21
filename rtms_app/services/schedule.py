from datetime import timedelta, date

from django.db import transaction

from rtms_app.models import TreatmentSession, Patient

try:
    import holidays as pyholidays
except Exception:
    pyholidays = None

# Module-level override used by tests to inject holiday dates (set of date objects)
EXTRA_HOLIDAYS: set[date] = set()


def _is_holiday(d: date) -> bool:
    if pyholidays:
        try:
            jp = pyholidays.CountryHoliday('JP')
            return d in jp
        except Exception:
            pass
    # Fallback to any test-injected holidays
    return d in EXTRA_HOLIDAYS


def is_treatment_day(d: date) -> bool:
    """Return True when `d` is a weekday and not a holiday."""
    if d.weekday() >= 5:
        return False
    return not _is_holiday(d)


def next_treatment_day(d: date) -> date:
    """Return the next date >= d that is a treatment day."""
    cur = d
    while not is_treatment_day(cur):
        cur = cur + timedelta(days=1)
    return cur


def _reflow_sessions(patient: Patient, course_number: int, source_date: date, target_date: date | None, moved_session: TreatmentSession | None):
    """
    Shared compaction routine used by both `shift_future_sessions` (skip/cancel path) and
    `reschedule_planned_session` (drag & drop). Compacts all 'planned' sessions for
    `patient`/`course_number` with `session_date > source_date` forward onto the next
    available treatment days, in their original relative order.

    If `moved_session` is given, it is (re)inserted at `target_date`, and all other
    sessions skip over `target_date` while being compacted. If `moved_session` is None,
    this simply closes the gap left at `source_date` (used when skipping/cancelling a
    session, which stays where it is but with a non-'planned' status).

    `discharge_date` is shifted by the same delta as the last repositioned session,
    when it is on or after `source_date`.

    Must be called within a transaction.
    """
    others_qs = (
        TreatmentSession.objects
        .filter(patient=patient, course_number=course_number, status='planned', session_date__gt=source_date)
        .order_by('session_date', 'id')
    )
    if moved_session is not None:
        others_qs = others_qs.exclude(pk=moved_session.pk)
    others = list(others_qs)

    if not others and moved_session is None:
        return {'affected_count': 0}

    original_last = others[-1].session_date if others else source_date

    cursor = source_date
    assignments = []  # (obj, original_date, new_date)
    for o in others:
        cursor = next_treatment_day(cursor + timedelta(days=1))
        if target_date is not None and cursor == target_date:
            cursor = next_treatment_day(cursor + timedelta(days=1))
        assignments.append((o, o.session_date, cursor))
    if moved_session is not None:
        assignments.append((moved_session, source_date, target_date))

    new_last = max((new for _, _, new in assignments), default=original_last)

    # Phase 1: move all affected rows to unique temporary placeholder dates to avoid
    # unique constraint collisions while dates are being reassigned.
    temp_base = date(1901, 1, 1)
    for i, (obj, _orig, _new) in enumerate(assignments):
        obj.session_date = temp_base + timedelta(days=i)
        obj.save(update_fields=['session_date'])

    # Phase 2: apply final dates, shifting the `.date` datetime by the same delta.
    for obj, orig, new in assignments:
        delta = new - orig
        try:
            if getattr(obj, 'date', None):
                obj.date = obj.date + delta
        except Exception:
            pass
        obj.session_date = new
        obj.save(update_fields=['session_date', 'date'])

    if getattr(patient, 'discharge_date', None) and patient.discharge_date >= source_date:
        delta = new_last - original_last
        patient.discharge_date = patient.discharge_date + delta
        patient.save(update_fields=['discharge_date'])

    return {'new_date': target_date.isoformat() if target_date else None, 'affected_count': len(assignments)}


def shift_future_sessions(patient: Patient, from_date: date, course_number: int):
    """
    Close the gap left by skipping/cancelling the session at `from_date`: all future
    'planned' TreatmentSession rows for the same patient/course (session_date > from_date)
    are compacted forward onto the next available treatment days (skip Sat/Sun and
    holidays), preserving their original relative order. `discharge_date` is shifted by
    the same delta as the last repositioned session, mirroring `reschedule_planned_session`.
    """
    if not patient:
        return
    with transaction.atomic():
        _reflow_sessions(patient, course_number, from_date, target_date=None, moved_session=None)


def reschedule_planned_session(patient: Patient, session: TreatmentSession, target_date: date):
    """
    Move a single 'planned' TreatmentSession (drag & drop postponement) to `target_date`,
    which must be a treatment day strictly after the session's current `session_date`.

    All other 'planned' sessions for the same patient/course that currently fall after the
    original `session_date` are compacted forward (in their original relative order) onto
    the next available treatment days, skipping over `target_date` (reserved for the moved
    session). `discharge_date` is shifted by the same delta as the last repositioned session,
    mirroring `shift_future_sessions`.

    Raises ValueError if `session.status != 'planned'`, `target_date` is not a treatment day,
    or `target_date` is not strictly after the session's current `session_date`.
    """
    if session.status != 'planned':
        raise ValueError("実施済み・スキップ済みの予定はこの方法では移動できません")
    if not is_treatment_day(target_date):
        raise ValueError("土日祝日には治療予定を設定できません")

    source_date = session.session_date
    if target_date <= source_date:
        raise ValueError("治療予定は後ろ倒し（未来の日付）にのみ移動できます")

    with transaction.atomic():
        result = _reflow_sessions(patient, session.course_number, source_date, target_date, moved_session=session)

    return {'moved_session_id': session.id, **result}
