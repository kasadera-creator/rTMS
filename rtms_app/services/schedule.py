from datetime import timedelta, date

from django.db import transaction

from rtms_app.models import TreatmentSession, Patient, MappingSchedule
from rtms_app.services.rtms_schedule import generate_treatment_dates, generate_mapping_dates


MAX_TREATMENT_SESSIONS = 30

try:
    import holidays as pyholidays
except Exception:
    pyholidays = None

# Module-level override used by tests to inject holiday dates (set of date objects)
EXTRA_HOLIDAYS: set[date] = set()


def get_treatment_sessions(patient, course_number=None):
    """Return all treatment rows in chronological order for one patient/course."""
    if course_number is None:
        course_number = patient.course_number or 1
    return list(
        TreatmentSession.objects.filter(
            patient=patient,
            course_number=course_number,
        ).order_by('session_date', 'id')
    )


def get_treatment_session_number_map(patient, course_number=None):
    """Map only the first 30 chronological sessions to treatment numbers."""
    sessions = get_treatment_sessions(patient, course_number)
    return {
        session.id: number
        for number, session in enumerate(sessions[:MAX_TREATMENT_SESSIONS], start=1)
    }


def get_treatment_virtual_number_map(patient, canonical_dates, course_number=None):
    """Number only the remaining canonical slots after materialized sessions."""
    materialized_count = min(
        len(get_treatment_sessions(patient, course_number)),
        MAX_TREATMENT_SESSIONS,
    )
    return {
        treatment_date: number
        for number, treatment_date in enumerate(
            canonical_dates[materialized_count:MAX_TREATMENT_SESSIONS],
            start=materialized_count + 1,
        )
    }


def get_treatment_overflow_info(patient, course_number=None):
    """Describe legacy rows beyond the 30-session treatment limit."""
    sessions = get_treatment_sessions(patient, course_number)
    overflow = sessions[MAX_TREATMENT_SESSIONS:]
    return {
        'count': len(sessions),
        'overflow_count': len(overflow),
        'has_overflow': bool(overflow),
        'overflow_sessions': overflow,
    }


def can_create_treatment_session(patient, course_number=None, additional=1):
    """Return whether adding ``additional`` rows stays within the 30-row limit."""
    if additional < 0:
        return False
    sessions = get_treatment_sessions(patient, course_number)
    return len(sessions) + additional <= MAX_TREATMENT_SESSIONS


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
    `patient`/`course_number` onto the next available treatment days, in their
    original relative order. For a manual move, the affected range starts at the
    earlier of the source and target dates; the moved session is the new anchor.

    If `moved_session` is given, it is inserted at `target_date` and every later
    planned session is rebuilt after it. If `moved_session` is None, this simply
    closes the gap left at `source_date` (used when skipping/cancelling a session,
    which stays where it is but with a non-'planned' status).

    `discharge_date` is shifted by the same delta as the last repositioned session,
    when it is on or after `source_date`.

    Must be called within a transaction.
    """
    if moved_session is not None:
        pivot_date = min(source_date, target_date)
        others_qs = (
            TreatmentSession.objects
            .filter(
                patient=patient,
                course_number=course_number,
                status='planned',
                session_date__gte=pivot_date,
            )
            .order_by('session_date', 'id')
        )
    else:
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

    assignments = []  # (obj, original_date, new_date)
    if moved_session is not None:
        # The moved session becomes the new anchor. Rebuild every planned session
        # from the affected range after it so treatment numbers remain chronological
        # in both directions.
        blocked_dates = set(
            TreatmentSession.objects.filter(
                patient=patient,
                course_number=course_number,
                status__in=['done', 'skipped'],
            ).exclude(pk=moved_session.pk).values_list('session_date', flat=True)
        )
        assignments.append((moved_session, source_date, target_date))
        cursor = target_date
        for o in others:
            cursor = next_treatment_day(cursor + timedelta(days=1))
            while cursor in blocked_dates:
                cursor = next_treatment_day(cursor + timedelta(days=1))
            assignments.append((o, o.session_date, cursor))
    else:
        cursor = source_date
        for o in others:
            cursor = next_treatment_day(cursor + timedelta(days=1))
            assignments.append((o, o.session_date, cursor))

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


def reschedule_planned_session(
    patient: Patient,
    session: TreatmentSession,
    target_date: date,
    *,
    allow_exceptional_day=False,
):
    """
    Move a single 'planned' TreatmentSession (drag & drop) to `target_date`, which must
    be a different treatment day from the session's current `session_date`.

    All other 'planned' sessions for the same patient/course in the affected range are
    rebuilt after `target_date` (in their original relative order) onto the next available
    treatment days. The same insertion rule is used for forward and backward moves.
    `discharge_date` is shifted by the same delta as the last
    repositioned session, mirroring `shift_future_sessions`.

    Raises ValueError if `session.status != 'planned'`, `target_date` is not a treatment day,
    or `target_date` is the same as the session's current `session_date`.
    """
    if session.status != 'planned':
        raise ValueError("実施済み・スキップ済みの予定はこの方法では移動できません")
    if not allow_exceptional_day and not is_treatment_day(target_date):
        raise ValueError("土日祝日には治療予定を設定できません")

    source_date = session.session_date
    if target_date == source_date:
        raise ValueError("移動先は現在の日付と異なる治療日を指定してください")

    conflict = TreatmentSession.objects.filter(
        patient=patient,
        course_number=session.course_number,
        session_date=target_date,
        status__in=['done', 'skipped'],
    ).exclude(pk=session.pk).exists()
    if conflict:
        raise ValueError("移動先には実施済みまたはスキップ済みの治療があります")

    with transaction.atomic():
        result = _reflow_sessions(patient, session.course_number, source_date, target_date, moved_session=session)

    return {'moved_session_id': session.id, **result}


def reschedule_treatment_start_date(
    patient: Patient,
    new_start_date: date,
    *,
    course_number=None,
    holidays=None,
    mapping_weeks=8,
    allow_exceptional_day=False,
):
    """Rebuild a course from a new first-treatment date.

    This is deliberately different from ``reschedule_planned_session``.  A
    first-session change changes the course baseline, so planned sessions are
    assigned to a newly generated canonical sequence.  Existing done/skipped
    rows are fixed records and are never moved or deleted.

    The service only updates already materialized planned rows.  It does not
    materialize missing rows.  If legacy overflow rows exist, the first 30
    chronological rows are treated as the regular course and overflow rows
    are deliberately left completely untouched.
    """
    if not isinstance(new_start_date, date):
        raise ValueError("初回治療日は正しい日付を指定してください")

    course_number = course_number or patient.course_number or 1
    if allow_exceptional_day and not is_treatment_day(new_start_date):
        generated_treatments = [new_start_date] + generate_treatment_dates(
            new_start_date + timedelta(days=1),
            total=MAX_TREATMENT_SESSIONS - 1,
            holidays=holidays,
        )
    else:
        generated_treatments = generate_treatment_dates(
            new_start_date, total=MAX_TREATMENT_SESSIONS, holidays=holidays,
        )
    if not generated_treatments or generated_treatments[0] != new_start_date:
        raise ValueError("初回治療日は土日祝日・休診日以外の日付を指定してください")

    with transaction.atomic():
        locked_patient = Patient.objects.select_for_update().get(pk=patient.pk)
        sessions = list(
            TreatmentSession.objects.select_for_update().filter(
                patient=locked_patient, course_number=course_number,
            ).order_by('session_date', 'id')
        )
        regular_sessions = sessions[:MAX_TREATMENT_SESSIONS]

        first_session = regular_sessions[0] if regular_sessions else None
        if first_session is not None and first_session.status != 'planned':
            raise ValueError("第1回が実施済みまたはスキップ済みのため、開始日を変更できません")

        fixed_dates = {
            session.session_date
            for session in regular_sessions
            if session.status in {'done', 'skipped'}
        }
        # Legacy overflow rows are immutable, but their dates still occupy a
        # unique TreatmentSession slot.  Do not move a regular row onto one of
        # those dates; extend the generated sequence only as far as needed to
        # place all regular rows without touching the overflow rows.
        blocked_dates = fixed_dates | {session.session_date for session in sessions[MAX_TREATMENT_SESSIONS:]}
        if new_start_date in fixed_dates:
            raise ValueError("新しい初回治療日が実施済みまたはスキップ済みの日付と重複しています")

        planned_sessions = [session for session in regular_sessions if session.status == 'planned']
        planned_dates = [d for d in generated_treatments if d not in blocked_dates]
        cursor = generated_treatments[-1] if generated_treatments else new_start_date
        while len(planned_dates) < len(planned_sessions):
            cursor = next_treatment_day(cursor + timedelta(days=1))
            if cursor not in blocked_dates:
                planned_dates.append(cursor)
        if len(planned_dates) < len(planned_sessions):
            raise ValueError("新しい開始日から治療予定を再構成できません")

        original_dates = {session.pk: session.session_date for session in planned_sessions}
        assignments = list(zip(planned_sessions, planned_dates))

        # Avoid transient collisions while changing several rows in one course.
        temporary_base = date(1901, 1, 1)
        for index, (session, _new_date) in enumerate(assignments):
            session.session_date = temporary_base + timedelta(days=index)
            session.save(update_fields=['session_date'])

        for session, new_date in assignments:
            old_date = original_dates[session.pk]
            delta = new_date - old_date
            if session.date:
                session.date = session.date + delta
            session.session_date = new_date
            session.save(update_fields=['session_date', 'date'])

        locked_patient.first_treatment_date = new_start_date
        locked_patient.mapping_date = new_start_date
        locked_patient.save(update_fields=['first_treatment_date', 'mapping_date'])

        generated_mapping = {
            item['week_no']: item['actual']
            for item in generate_mapping_dates(
                new_start_date, weeks=mapping_weeks, holidays=holidays,
            )
        }
        for schedule in MappingSchedule.objects.select_for_update().filter(
            patient=locked_patient, course_number=course_number,
        ):
            if schedule.week_number in generated_mapping:
                schedule.planned_date = generated_mapping[schedule.week_number]
                schedule.save(update_fields=['planned_date', 'updated_at'])

    return {
        'old_start_date': patient.first_treatment_date,
        'new_start_date': new_start_date,
        'moved_count': len(assignments),
        'mapping_rebuilt': bool(generated_mapping),
    }
