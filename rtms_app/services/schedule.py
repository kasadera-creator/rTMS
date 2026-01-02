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


def shift_future_sessions(patient: Patient, from_date: date):
    """
    Reschedule all future planned TreatmentSession for the patient (session_date > from_date)
    so that they fall on the next available treatment days (skip Sat/Sun and holidays).

    The algorithm preserves order: starting from `from_date`, the first future planned
    session will be moved to the next treatment day after `from_date`, the next planned
    session will be moved to the next treatment day after that, and so on.

    If the patient's `discharge_date` is on or after `from_date`, it will be moved to
    the last assigned session_date.
    """
    if not patient:
        return

    with transaction.atomic():
        futures_qs = (
            TreatmentSession.objects
            .filter(patient=patient, session_date__gt=from_date, status='planned')
            .order_by('session_date', 'id')
        )

        # Capture original last planned session date (if any) to compute discharge delta
        original_last = None
        if futures_qs.exists():
            original_last = futures_qs.order_by('-session_date', '-id').first().session_date

        anchor = from_date
        last_assigned = None
        # Compute target dates first (in ascending order)
        futures = list(futures_qs)
        targets = []
        for ts in futures:
            orig = ts.session_date
            next_date = next_treatment_day(anchor + timedelta(days=1))
            targets.append(next_date)
            anchor = next_date
        

        # Save in reverse order to avoid unique constraint collisions
        for ts, target in zip(reversed(futures), reversed(targets)):
            orig = ts.session_date
            try:
                if getattr(ts, 'date', None):
                    ts.date = ts.date + (target - orig)
            except Exception:
                pass
            try:
                ts.session_date = target
                ts.save(update_fields=['session_date', 'date'])
            except Exception:
                raise

        if targets:
            last_assigned = targets[-1]

        # Shift discharge_date by the same delta as the last planned session moved
        if getattr(patient, 'discharge_date', None) and last_assigned and original_last:
            try:
                if patient.discharge_date >= from_date:
                    delta = last_assigned - original_last
                    patient.discharge_date = patient.discharge_date + delta
                    patient.save(update_fields=['discharge_date'])
            except Exception as e:
                raise
