from datetime import date, datetime, timedelta

from django.db import IntegrityError
from django.utils import timezone

from rtms_app.models import (
    MappingSchedule,
    RtmSTreatmentReservation,
    TreatmentSession,
    TreatmentSkip,
)
from rtms_app.services.rtms_treatment_reservations import (
    ACTIVE_RESERVATION_STATUSES,
    TreatmentReservationBusyError,
    sync_rtms_treatment_reservation_from_sessions,
    treatment_capacity_snapshots,
)
from rtms_app.services.schedule import (
    _save_course_aware_session,
    next_treatment_day,
)
from rtms_app.services.session_writes import (
    PatientCourseWriteError,
    patient_course_writer,
    validate_session_course,
)
from rtms_app.services.treatment_cancellation import _run_with_cancellation_busy_retry


class TreatmentSkipUndoError(ValueError):
    """Raised when a normal skip cannot be safely restored."""


V2_SNAPSHOT_MARKER_KEYS = frozenset({
    "patient_id",
    "treatment_course_id",
    "course_number",
    "all_sessions_before",
    "affected_sessions_before",
    "reservations_before",
    "mapping_schedules_before",
    "discharge_before",
    "skip_session_after",
    "all_sessions_after",
    "affected_sessions_after",
    "reservations_after",
    "mapping_schedules_after",
    "discharge_after",
})


def _session_state(session):
    return {
        "pk": session.pk,
        "fields": {
            field.attname: field.value_to_string(session)
            for field in session._meta.concrete_fields
        },
    }


def _mapping_scope(patient, treatment_course, course_number):
    if treatment_course is not None:
        return {"treatment_course": treatment_course}
    return {
        "patient": patient,
        "course_number": course_number,
        "treatment_course__isnull": True,
    }


def _mapping_schedule_states(patient, treatment_course, course_number):
    return [
        {
            "pk": row.pk,
            "patient_id": row.patient_id,
            "treatment_course_id": row.treatment_course_id,
            "course_number": row.course_number,
            "week_number": row.week_number,
            "planned_date": row.planned_date.isoformat(),
        }
        for row in MappingSchedule.objects.select_for_update()
        .filter(**_mapping_scope(patient, treatment_course, course_number))
        .order_by("pk")
    ]


def _reservation_states(treatment_course):
    if treatment_course is None:
        return []
    return [
        {
            "pk": row.pk,
            "treatment_course_id": row.treatment_course_id,
            "treatment_date": row.treatment_date.isoformat(),
            "sequence_number": row.sequence_number,
            "status": row.status,
            "capacity_type": row.capacity_type,
        }
        for row in RtmSTreatmentReservation.objects.select_for_update()
        .filter(treatment_course=treatment_course)
        .order_by("pk")
    ]


def _discharge_state(patient, treatment_course):
    return {
        "patient_discharge_date": (
            patient.discharge_date.isoformat() if patient.discharge_date else None
        ),
        "course_discharge_date": (
            treatment_course.discharge_date.isoformat()
            if treatment_course is not None and treatment_course.discharge_date
            else None
        ),
    }


def capture_skip_undo_before(
    *, patient, treatment_course, course_number, affected_sessions,
):
    session_scope = _mapping_scope(patient, treatment_course, course_number)
    all_sessions = list(
        TreatmentSession.objects.select_for_update()
        .filter(**session_scope)
        .order_by("session_date", "pk")
    )
    return {
        "undo_snapshot_version": 2,
        "patient_id": patient.pk,
        "treatment_course_id": treatment_course.pk if treatment_course else None,
        "course_number": course_number,
        "all_sessions_before": [_session_state(row) for row in all_sessions],
        "affected_sessions_before": [
            _session_state(row) for row in affected_sessions
        ],
        "reservations_before": _reservation_states(treatment_course),
        "mapping_schedules_before": _mapping_schedule_states(
            patient, treatment_course, course_number,
        ),
        "discharge_before": _discharge_state(patient, treatment_course),
    }


def capture_skip_undo_after(
    *, skip, patient, treatment_course, course_number,
):
    session_scope = _mapping_scope(patient, treatment_course, course_number)
    all_sessions = list(
        TreatmentSession.objects.select_for_update()
        .filter(**session_scope)
        .order_by("session_date", "pk")
    )
    snapshot = dict(skip.snapshot or {})
    snapshot.update({
        "skip_session_after": _session_state(
            next(row for row in all_sessions if row.pk == skip.treatment_id)
        ),
        "all_sessions_after": [_session_state(row) for row in all_sessions],
        "affected_sessions_after": [
            _session_state(row)
            for row in all_sessions
            if row.session_date > skip.effective_date
        ],
        "reservations_after": _reservation_states(treatment_course),
        "mapping_schedules_after": _mapping_schedule_states(
            patient, treatment_course, course_number,
        ),
        "discharge_after": _discharge_state(patient, treatment_course),
    })
    skip.snapshot = snapshot
    skip.save(update_fields=["snapshot"])


def _fail(message):
    raise TreatmentSkipUndoError(message)


def _parse_date(value, label):
    if not isinstance(value, str):
        _fail(f"{label}snapshotの日付形式が不正です。")
    try:
        return date.fromisoformat(value)
    except ValueError:
        _fail(f"{label}snapshotの日付形式が不正です。")


def _parse_datetime(value, label):
    if value is None:
        return None
    if not isinstance(value, str):
        _fail(f"{label}snapshotの日時形式が不正です。")
    try:
        if "T" not in value:
            return datetime.combine(date.fromisoformat(value), datetime.min.time())
        return datetime.fromisoformat(value)
    except ValueError:
        _fail(f"{label}snapshotの日時形式が不正です。")


def _snapshot_rows(value, label):
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        _fail(f"{label}snapshotの形式が不正です。")
    ids = [item.get("pk") for item in value]
    if (
        any(not isinstance(item, int) or isinstance(item, bool) for item in ids)
        or len(ids) != len(set(ids))
    ):
        _fail(f"{label}snapshotのIDが不正です。")
    return {item["pk"]: item for item in value}


def _session_rows(writer):
    return list(
        TreatmentSession.objects.select_for_update()
        .filter(**writer.session_filter)
        .order_by("session_date", "pk")
    )


def _expected_reflow(before_by_id, skipped_session_id, skipped_date):
    future = []
    fixed_dates = set()
    for session_id, state in before_by_id.items():
        fields = state.get("fields")
        if not isinstance(fields, dict):
            _fail("Session snapshotの形式が不正です。")
        original_date = _parse_date(fields.get("session_date"), "Session ")
        status = fields.get("status")
        if session_id == skipped_session_id:
            if status != "planned":
                _fail("skip前Sessionの状態がplannedではありません。")
            continue
        if original_date <= skipped_date:
            continue
        if status == "planned":
            future.append((original_date, session_id, fields))
        elif status in {"done", "skipped"}:
            fixed_dates.add(original_date)
        else:
            _fail("Session snapshotの状態が不正です。")

    targets = {}
    cursor = skipped_date
    for original_date, session_id, fields in sorted(future):
        cursor = next_treatment_day(cursor + timedelta(days=1))
        while cursor in fixed_dates:
            cursor = next_treatment_day(cursor + timedelta(days=1))
        targets[session_id] = (original_date, cursor, fields)
    return targets, fixed_dates


def _validate_state_transition(before_by_id, after_by_id, skipped_session_id, skipped_date):
    if set(before_by_id) != set(after_by_id):
        _fail("skip後にSessionが追加または削除されています。")
    targets, fixed_dates = _expected_reflow(
        before_by_id, skipped_session_id, skipped_date,
    )
    last_original = max((item[0] for item in targets.values()), default=skipped_date)
    last_target = max((item[1] for item in targets.values()), default=last_original)

    for session_id, before in before_by_id.items():
        after = after_by_id[session_id]
        before_fields = before.get("fields")
        after_fields = after.get("fields")
        if not isinstance(before_fields, dict) or not isinstance(after_fields, dict):
            _fail("Session snapshotの形式が不正です。")
        if session_id == skipped_session_id:
            expected_fields = dict(before_fields)
            expected_fields["status"] = "skipped"
            if after_fields != expected_fields:
                _fail("skip対象Sessionがskip後に変更されています。")
            continue

        target = targets.get(session_id)
        if target is None:
            if after_fields != before_fields:
                _fail("done/skipped Sessionがskip後に変更されています。")
            continue

        original_date, target_date, _ = target
        if any(
            after_fields.get(name) != before_fields.get(name)
            for name in ("patient_id", "treatment_course_id", "course_number", "slot")
        ):
            _fail("後続Sessionがskip後に変更されています。")
        if _parse_date(after_fields.get("session_date"), "Session ") != target_date:
            _fail("後続Sessionの日付がskip後snapshotと一致しません。")
        original_datetime = _parse_datetime(before_fields.get("date"), "Session ")
        actual_datetime = _parse_datetime(after_fields.get("date"), "Session ")
        expected_datetime = (
            original_datetime + (target_date - original_date)
            if original_datetime is not None else None
        )
        if actual_datetime != expected_datetime:
            _fail("後続Sessionの日時がskip後snapshotと一致しません。")

    return targets, last_target - last_original, fixed_dates


def _validate_v2_snapshot(skip, writer, session, snapshot, current_sessions):
    if (
        snapshot.get("patient_id") != writer.patient.pk
        or snapshot.get("course_number") != writer.course_number
        or snapshot.get("treatment_course_id") != (
            writer.treatment_course.pk if writer.treatment_course else None
        )
    ):
        _fail("skip snapshotのPatient/Course所有関係が一致しません。")

    before_by_id = _snapshot_rows(snapshot.get("all_sessions_before"), "skip前Session")
    after_by_id = _snapshot_rows(snapshot.get("all_sessions_after"), "skip後Session")
    affected_before = _snapshot_rows(
        snapshot.get("affected_sessions_before"), "対象Session",
    )
    affected_after = _snapshot_rows(
        snapshot.get("affected_sessions_after"), "skip後対象Session",
    )
    expected_affected_before = {
        session_id: state for session_id, state in before_by_id.items()
        if _parse_date(state.get("fields", {}).get("session_date"), "Session ")
        > skip.effective_date
    }
    expected_affected_after = {
        session_id: state for session_id, state in after_by_id.items()
        if _parse_date(state.get("fields", {}).get("session_date"), "Session ")
        > skip.effective_date
    }
    if (
        set(before_by_id) != set(after_by_id)
        or affected_before != expected_affected_before
        or affected_after != expected_affected_after
    ):
        _fail("skip snapshotのSession対象が一致しません。")

    actual_by_id = {row.pk: _session_state(row) for row in current_sessions}
    if actual_by_id != after_by_id:
        _fail("Sessionがskip後に変更されています。安全のためundoできません。")
    selected_state = snapshot.get("skip_session_after")
    if not isinstance(selected_state, dict) or actual_by_id.get(session.pk) != selected_state:
        _fail("skip対象Session snapshotが一致しません。")

    targets, delta, fixed_dates = _validate_state_transition(
        before_by_id, after_by_id, session.pk, skip.effective_date,
    )
    for state in before_by_id.values():
        fields = state["fields"]
        if (
            fields.get("patient_id") != str(writer.patient.pk)
            or fields.get("course_number") != str(writer.course_number)
            or fields.get("treatment_course_id") != str(
                writer.treatment_course.pk if writer.treatment_course else None
            )
        ):
            _fail("Session snapshotのPatient/Course所有関係が一致しません。")

    reservations_before = _snapshot_rows(
        snapshot.get("reservations_before"), "skip前Reservation",
    )
    reservations_after = _snapshot_rows(
        snapshot.get("reservations_after"), "skip後Reservation",
    )
    mapping_before = _snapshot_rows(
        snapshot.get("mapping_schedules_before"), "skip前MappingSchedule",
    )
    mapping_after = _snapshot_rows(
        snapshot.get("mapping_schedules_after"), "skip後MappingSchedule",
    )
    if not isinstance(snapshot.get("discharge_before"), dict) or not isinstance(
        snapshot.get("discharge_after"), dict
    ):
        _fail("skip snapshotにdischarge状態がありません。")

    if reservations_after != {
        row["pk"]: row for row in _reservation_states(writer.treatment_course)
    }:
        _fail("Reservationがskip後に変更されています。安全のためundoできません。")
    if mapping_after != {
        row["pk"]: row for row in _mapping_schedule_states(
            writer.patient, writer.treatment_course, writer.course_number,
        )
    }:
        _fail("MappingScheduleがskip後に変更されています。安全のためundoできません。")
    if not set(reservations_before).issubset(reservations_after):
        _fail("skip前Reservationが削除されています。")
    if not set(mapping_before).issubset(mapping_after):
        _fail("skip前MappingScheduleが削除されています。")
    for row in mapping_before.values():
        if (
            row.get("patient_id") != writer.patient.pk
            or row.get("course_number") != writer.course_number
            or row.get("treatment_course_id") != (
                writer.treatment_course.pk if writer.treatment_course else None
            )
        ):
            _fail("skip前MappingScheduleのPatient/Course所有関係が一致しません。")
        _parse_date(row.get("planned_date"), "MappingSchedule ")
    for row in reservations_before.values():
        if (
            row.get("treatment_course_id") != writer.treatment_course.pk
            or not isinstance(row.get("sequence_number"), int)
            or isinstance(row.get("sequence_number"), bool)
            or row.get("status") not in {*ACTIVE_RESERVATION_STATUSES, "cancelled"}
        ):
            _fail("skip前ReservationのCourseまたは状態が不正です。")

    before_discharge = snapshot["discharge_before"]
    after_discharge = snapshot["discharge_after"]
    if _discharge_state(writer.patient, writer.treatment_course) != after_discharge:
        _fail("discharge dateがskip後に変更されています。")
    return (
        targets, delta, fixed_dates, reservations_before, before_discharge,
        mapping_before, mapping_after,
    )


def _validate_legacy_snapshot(skip, writer, session, snapshot, current_sessions):
    if not isinstance(snapshot.get("affected_sessions"), list):
        _fail("旧形式skip snapshotがありません。安全のためundoできません。")
    if (
        snapshot.get("skipped_session_id", session.pk) != session.pk
        or snapshot.get("skipped_session_date", skip.effective_date.isoformat())
        != skip.effective_date.isoformat()
    ):
        _fail("旧形式skip snapshotと対象Sessionが一致しません。")

    affected = snapshot["affected_sessions"]
    if any(not isinstance(item, dict) for item in affected):
        _fail("旧形式skip snapshotの対象Sessionが不正です。")
    ids = [item.get("id") for item in affected]
    if (
        any(not isinstance(item, int) or isinstance(item, bool) for item in ids)
        or len(ids) != len(set(ids))
        or session.pk in ids
    ):
        _fail("旧形式skip snapshotのSession IDが不正です。")

    current_by_id = {row.pk: row for row in current_sessions}
    future_ids = {
        row.pk for row in current_sessions if row.session_date > skip.effective_date
    }
    if future_ids != set(ids):
        _fail("旧形式skip後にSessionが追加・削除・再配置されています。")

    before_by_id = {}
    for item in affected:
        original_date = _parse_date(item.get("session_date"), "旧形式Session ")
        original_datetime = _parse_datetime(item.get("date"), "旧形式Session ")
        row = current_by_id.get(item["id"])
        if row is None:
            _fail("旧形式skip対象Sessionが見つかりません。")
        validate_session_course(row, writer.patient, writer.course_number, writer.treatment_course)
        if row.status != "planned":
            _fail("旧形式skip対象Sessionの状態が不正です。")
        before_by_id[row.pk] = {
            "pk": row.pk,
            "fields": {
                "session_date": original_date.isoformat(),
                "date": original_datetime.isoformat() if original_datetime else None,
                "status": row.status,
                "patient_id": str(row.patient_id),
                "treatment_course_id": (
                    str(row.treatment_course_id) if row.treatment_course_id else None
                ),
                "course_number": str(row.course_number),
            },
        }

    targets, fixed_dates = _expected_reflow(
        before_by_id | {
            session.pk: {
                "pk": session.pk,
                "fields": {
                    "session_date": skip.effective_date.isoformat(),
                    "status": "planned",
                },
            },
        },
        session.pk,
        skip.effective_date,
    )
    last_original = max((item[0] for item in targets.values()), default=skip.effective_date)
    last_target = max((item[1] for item in targets.values()), default=last_original)
    delta = last_target - last_original
    for session_id, (original_date, target_date, _) in targets.items():
        row = current_by_id[session_id]
        if row.session_date != target_date:
            _fail("旧形式skip後の日付を検証できません。安全のためundoできません。")
        original_datetime = _parse_datetime(
            next(item["date"] for item in affected if item["id"] == session_id),
            "旧形式Session ",
        )
        expected_datetime = (
            original_datetime + (target_date - original_date)
            if original_datetime is not None else None
        )
        if row.date != expected_datetime:
            _fail("旧形式skip後の日時を検証できません。安全のためundoできません。")

    if writer.treatment_course is not None and RtmSTreatmentReservation.objects.filter(
        treatment_course=writer.treatment_course,
    ).exists():
        _fail("旧形式skipではReservationの変更有無を検証できません。安全のためundoできません。")
    if _mapping_schedule_states(
        writer.patient, writer.treatment_course, writer.course_number,
    ):
        _fail("旧形式skipではMappingSchedule状態を検証できません。安全のためundoできません。")

    before_discharge = {
        "patient_discharge_date": snapshot.get("patient_discharge_date"),
        "course_discharge_date": snapshot.get("course_discharge_date"),
    }
    for key in ("patient_discharge_date", "course_discharge_date"):
        value = before_discharge[key]
        if value is not None:
            _parse_date(value, "旧形式discharge ")
    expected_after = {}
    for key, value in before_discharge.items():
        original = date.fromisoformat(value) if value else None
        expected = original + delta if original and original >= skip.effective_date else original
        expected_after[key] = expected.isoformat() if expected else None
    if _discharge_state(writer.patient, writer.treatment_course) != expected_after:
        _fail("旧形式skip後のdischarge dateを検証できません。")
    return targets, delta, fixed_dates, {}, before_discharge, {}, {}


def _validate_restore_dates(writer, session, targets, fixed_dates):
    restoring_ids = set(targets) | {session.pk}
    target_slots = set()
    for target_date, session_id in (
        (target[0], session_id) for session_id, target in targets.items()
    ):
        row = TreatmentSession.objects.get(pk=session_id)
        key = (target_date, row.slot)
        if key in target_slots:
            _fail("復元先Sessionの日付が重複しています。")
        target_slots.add(key)

    for target_date, slot in target_slots:
        collision = TreatmentSession.objects.filter(
            **writer.session_filter,
            session_date=target_date,
            slot=slot,
        ).exclude(pk__in=restoring_ids).exists()
        if collision:
            _fail("復元先に同一CourseのSessionがあるためundoできません。")
        if target_date in fixed_dates:
            _fail("復元先がdone/skipped Sessionと衝突します。")


def _restore_sessions(writer, session, target_states):
    rows = {row.pk: row for row in _session_rows(writer)}
    restoring = []
    occupied_dates = {row.session_date for row in rows.values()}
    candidate = date(1901, 1, 1)
    for session_id, state in target_states.items():
        row = rows.get(session_id)
        if row is None:
            _fail("復元対象Sessionが見つかりません。")
        fields = state["fields"]
        if fields.get("status") != "planned":
            continue
        original_date = _parse_date(fields.get("session_date"), "skip前Session ")
        original_datetime = _parse_datetime(fields.get("date"), "skip前Session ")
        while candidate in occupied_dates:
            candidate += timedelta(days=1)
        occupied_dates.add(candidate)
        row.session_date = candidate
        _save_course_aware_session(row, writer.patient)
        restoring.append((row, original_date, original_datetime))
        candidate += timedelta(days=1)

    for row, original_date, original_datetime in restoring:
        row.session_date = original_date
        row.date = original_datetime
        _save_course_aware_session(row, writer.patient)

    session.status = "planned"
    session.save(update_fields=["status"])


def _restore_reservation_statuses(writer, reservations_before):
    if writer.treatment_course is None:
        return
    current = {
        row.pk: row for row in RtmSTreatmentReservation.objects.select_for_update()
        .filter(treatment_course=writer.treatment_course)
    }
    for reservation_id, state in reservations_before.items():
        row = current.get(reservation_id)
        if row is None or row.treatment_course_id != writer.treatment_course.pk:
            _fail("skip前Reservationが見つからないかCourseが一致しません。")
        fields = state
        if fields["status"] not in {*ACTIVE_RESERVATION_STATUSES, "cancelled"}:
            _fail("skip前Reservation statusが不正です。")
        row.status = fields["status"]
        row.save(update_fields=["status", "updated_at"])


def _restore_mapping_schedules(writer, mapping_before, mapping_after):
    current = {
        row.pk: row for row in MappingSchedule.objects.select_for_update()
        .filter(**_mapping_scope(
            writer.patient, writer.treatment_course, writer.course_number,
        ))
    }
    for schedule_id, state in mapping_before.items():
        row = current.get(schedule_id)
        if row is None:
            _fail("skip前MappingScheduleが見つかりません。")
        row.planned_date = _parse_date(state["planned_date"], "MappingSchedule ")
        row.save(update_fields=["planned_date", "updated_at"])

    for schedule_id in mapping_after.keys() - mapping_before.keys():
        row = current.get(schedule_id)
        if row is None:
            _fail("skip後MappingScheduleが見つかりません。")
        row.delete()


def _validate_reservation_capacity(writer):
    if writer.treatment_course is None:
        return
    reservations = list(RtmSTreatmentReservation.objects.select_for_update().filter(
        treatment_course=writer.treatment_course,
        status__in=ACTIVE_RESERVATION_STATUSES,
    ))
    snapshots = dict(zip(
        dict.fromkeys(row.treatment_date for row in reservations),
        treatment_capacity_snapshots(
            dict.fromkeys(row.treatment_date for row in reservations),
        ),
    ))
    for reservation in reservations:
        snapshot = snapshots[reservation.treatment_date]
        capacity = snapshot[reservation.capacity_type]["capacity"]
        occupied = snapshot[reservation.capacity_type]["occupied"]
        if capacity is None or occupied > capacity:
            _fail("undo後の治療予約が容量を超えるため復元できません。")


def _write_discharge_before(writer, before):
    patient_value = before.get("patient_discharge_date")
    course_value = before.get("course_discharge_date")
    patient_date = date.fromisoformat(patient_value) if patient_value else None
    course_date = date.fromisoformat(course_value) if course_value else None
    if writer.treatment_course is not None:
        writer.treatment_course.discharge_date = course_date
        writer.treatment_course.save(update_fields=["discharge_date"])
        if writer.course_number == 1:
            writer.patient.discharge_date = patient_date
            writer.patient.save(update_fields=["discharge_date"])
    else:
        writer.patient.discharge_date = patient_date
        writer.patient.save(update_fields=["discharge_date"])


def _undo_locked(
    *, skip_id, patient_id, course_number, treatment_course_id, treatment_id, user,
):
    with patient_course_writer(
        patient_id,
        course_number,
        expected_course_id=treatment_course_id,
        require_course=treatment_course_id is not None,
    ) as writer:
        if writer.patient.pk != patient_id or writer.course_number != course_number:
            _fail("Patient/Courseがundo対象と一致しません。")
        skip = TreatmentSkip.objects.select_for_update().filter(pk=skip_id).first()
        if (
            skip is None
            or skip.action_type != "postpone"
            or skip.batch_id is not None
            or skip.undone_at is not None
            or skip.treatment_id != treatment_id
        ):
            _fail("通常skip記録が未処理状態ではありません。")
        snapshot = skip.snapshot
        if not isinstance(snapshot, dict):
            _fail("skip snapshotがありません。")
        if skip.effective_date is None:
            _fail("skip対象日がありません。")
        session = TreatmentSession.objects.select_for_update().filter(
            **writer.session_filter,
            pk=skip.treatment_id,
        ).first()
        if session is None:
            _fail("skip対象Sessionが同一Patient/Courseにありません。")
        validate_session_course(
            session, writer.patient, writer.course_number, writer.treatment_course,
        )
        if session.status != "skipped" or session.session_date != skip.effective_date:
            _fail("skip対象Sessionがskip後状態と一致しません。")
        has_version = "undo_snapshot_version" in snapshot
        version = snapshot.get("undo_snapshot_version")
        default_session_id = session.pk if version is None else None
        default_session_date = (
            skip.effective_date.isoformat() if version is None else None
        )
        if (
            snapshot.get("skipped_session_id", default_session_id) != session.pk
            or snapshot.get("skipped_session_date", default_session_date)
            != skip.effective_date.isoformat()
        ):
            _fail("skip snapshotと対象Sessionが一致しません。")

        current_sessions = _session_rows(writer)
        if isinstance(version, int) and not isinstance(version, bool) and version == 2:
            (
                targets, _delta, fixed_dates, reservations_before, discharge_before,
                mapping_before, mapping_after,
            ) = (
                _validate_v2_snapshot(
                    skip, writer, session, snapshot, current_sessions,
                )
            )
            target_states = _snapshot_rows(
                snapshot.get("affected_sessions_before"), "skip前対象Session",
            )
            reservations_before = _snapshot_rows(
                snapshot.get("reservations_before"), "skip前Reservation",
            )
        elif not has_version:
            if V2_SNAPSHOT_MARKER_KEYS.intersection(snapshot):
                _fail("version情報のないv2形式skip snapshotはundoできません。")
            (
                targets, _delta, fixed_dates, reservations_before, discharge_before,
                mapping_before, mapping_after,
            ) = (
                _validate_legacy_snapshot(
                    skip, writer, session, snapshot, current_sessions,
                )
            )
            target_states = {}
            for item in snapshot["affected_sessions"]:
                row = next(row for row in current_sessions if row.pk == item["id"])
                target_states[row.pk] = {
                    "pk": row.pk,
                    "fields": {
                        "session_date": item["session_date"],
                        "date": item.get("date"),
                        "status": row.status,
                    },
                }
        else:
            _fail("skip snapshot versionが不正です。")

        _validate_restore_dates(writer, session, targets, fixed_dates)
        _restore_reservation_statuses(writer, reservations_before)
        _restore_sessions(writer, session, target_states)
        _write_discharge_before(writer, discharge_before)

        if writer.treatment_course is not None:
            sync_rtms_treatment_reservation_from_sessions(
                treatment_course=writer.treatment_course,
                user=user,
            )
            _restore_reservation_statuses(writer, reservations_before)
            if {
                row["pk"]: row
                for row in _reservation_states(writer.treatment_course)
            } != reservations_before:
                _fail("undo後Reservationがskip前状態に復元されませんでした。")
            _validate_reservation_capacity(writer)

        _restore_mapping_schedules(writer, mapping_before, mapping_after)

        skip.undone_by = user
        skip.undone_at = timezone.now()
        skip.save(update_fields=["undone_by", "undone_at"])
        return writer.patient.pk


def undo_treatment_skip(
    *, skip_id, patient_id, course_number, treatment_course_id, treatment_id, user,
):
    try:
        return _run_with_cancellation_busy_retry(
            lambda: _undo_locked(
                skip_id=skip_id,
                patient_id=patient_id,
                course_number=course_number,
                treatment_course_id=treatment_course_id,
                treatment_id=treatment_id,
                user=user,
            ),
            operation_owns_atomic=True,
        )
    except (TreatmentSkipUndoError, TreatmentReservationBusyError):
        raise
    except (IntegrityError, PatientCourseWriteError, ValueError) as exc:
        raise TreatmentSkipUndoError(str(exc)) from exc