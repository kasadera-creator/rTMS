"""Transactional cancellation and undo for treatment sessions."""

import time
from datetime import date, datetime, timedelta
from uuid import uuid4

from django.db import OperationalError, connection, transaction

from rtms_app.models import (
    RtmSTreatmentReservation,
    TreatmentSession,
    TreatmentSkip,
)
from rtms_app.services.rtms_treatment_reservations import (
    CAPACITY_TYPES,
    RESERVATION_BUSY_BACKOFF_SECONDS,
    RESERVATION_BUSY_RETRIES,
    TreatmentReservationBusyError,
    _is_sqlite_busy_error,
    _occupancy_for_dates,
    _settings_for_dates,
    sync_rtms_treatment_reservation_from_sessions,
)
from rtms_app.services.session_writes import patient_course_writer, validate_session_course


class TreatmentCancellationError(ValueError):
    """Raised when cancellation or undo cannot be applied without data loss."""


SNAPSHOT_VERSION = 2
LEGACY_SNAPSHOT_VERSION = 1


def _date_text(value):
    return value.isoformat() if value is not None else None


def _session_snapshot(session, *, before_status=None):
    fields = {
        field.attname: field.value_to_string(session)
        for field in session._meta.concrete_fields
    }
    if before_status is not None:
        fields["status"] = before_status
    return {
        "id": session.pk,
        "patient_id": session.patient_id,
        "treatment_course_id": session.treatment_course_id,
        "course_number": session.course_number,
        "session_date": _date_text(session.session_date),
        "date": session.date.isoformat() if session.date else None,
        "status": before_status if before_status is not None else session.status,
        "fields": fields,
    }


def _reservation_snapshot(reservations):
    return [
        {
            "id": reservation.pk,
            "sequence_number": reservation.sequence_number,
            "treatment_date": reservation.treatment_date.isoformat(),
            "status": reservation.status,
            "capacity_type": reservation.capacity_type,
            "updated_at": reservation.updated_at.isoformat(),
        }
        for reservation in reservations
    ]


def _snapshot_discharge(patient, treatment_course):
    return {
        "course_discharge_date": _date_text(
            treatment_course.discharge_date if treatment_course else None
        ),
        "patient_discharge_date": _date_text(
            patient.discharge_date
            if treatment_course is None or treatment_course.course_number == 1
            else None
        ),
    }


def _write_discharge(patient, treatment_course, value):
    if treatment_course is not None:
        treatment_course.discharge_date = value
        treatment_course.save(update_fields=["discharge_date"])
        if treatment_course.course_number == 1:
            patient.discharge_date = value
            patient.save(update_fields=["discharge_date"])
    else:
        patient.discharge_date = value
        patient.save(update_fields=["discharge_date"])


def _run_with_cancellation_busy_retry(
    operation,
    *,
    operation_owns_atomic=False,
    error_message="同時処理のため中止・取消解除を保存できませんでした。もう一度お試しください。",
):
    for attempt in range(RESERVATION_BUSY_RETRIES + 1):
        try:
            if operation_owns_atomic:
                return operation()
            with transaction.atomic():
                return operation()
        except (OperationalError, TreatmentReservationBusyError) as exc:
            if not _is_sqlite_busy_error(exc):
                raise
            if attempt == RESERVATION_BUSY_RETRIES:
                raise TreatmentReservationBusyError(
                    error_message
                ) from exc
            time.sleep(RESERVATION_BUSY_BACKOFF_SECONDS[attempt])


def cancel_treatment_sessions(
    *, patient_id, course_number, treatment_date, user, reason="", expected_course_id=None,
):
    """Mark planned sessions from the selected date as skipped and preserve history."""
    def operation():
        with patient_course_writer(
            patient_id,
            course_number,
            expected_course_id=expected_course_id,
        ) as writer:
            patient = writer.patient
            treatment_course = writer.treatment_course
            scope = writer.session_filter
            sessions = list(
                TreatmentSession.objects.select_for_update()
                .filter(**scope, session_date__gte=treatment_date)
                .order_by("session_date", "pk")
            )
            selected = [
                item for item in sessions
                if item.session_date == treatment_date and item.slot == ""
            ]
            if len(selected) != 1:
                raise TreatmentCancellationError("対象の治療予定を一意に確認できません。")
            selected_session = selected[0]
            validate_session_course(
                selected_session, patient, writer.course_number, treatment_course,
            )
            if selected_session.status not in {"planned", "done", "skipped"}:
                raise TreatmentCancellationError(
                    "対象Sessionの状態が中止処理に対応していません。"
                )
            if TreatmentSkip.objects.select_for_update().filter(
                treatment=selected_session,
                action_type="cancel",
                undone_at__isnull=True,
            ).exists():
                raise TreatmentCancellationError("この治療予定はすでに中止されています。")
            batch_id = uuid4()
            target_sessions = [item for item in sessions if item.status == "planned"]

            reservations = []
            if treatment_course is not None:
                reservations = list(
                    RtmSTreatmentReservation.objects.select_for_update()
                    .filter(treatment_course=treatment_course)
                    .order_by("sequence_number", "pk")
                )
            before_discharge = _snapshot_discharge(patient, treatment_course)
            snapshot = {
                "version": SNAPSHOT_VERSION,
                "kind": "cancel",
                "batch_role": "root",
                "batch_id": str(batch_id),
                "patient_id": patient.pk,
                "treatment_course_id": treatment_course.pk if treatment_course else None,
                "course_number": writer.course_number,
                "selected_session_id": selected_session.pk,
                "effective_date": treatment_date.isoformat(),
                "target_session_ids": [item.pk for item in target_sessions],
                "affected_sessions_before": [_session_snapshot(item) for item in sessions],
                "reservations_before": _reservation_snapshot(reservations),
                "discharge_before": before_discharge,
            }

            for session in sessions:
                if session.status == "planned":
                    session.status = "skipped"
                    session.save(update_fields=["status"])

            _write_discharge(patient, treatment_course, treatment_date)
            if treatment_course is not None:
                sync_rtms_treatment_reservation_from_sessions(
                    treatment_course=treatment_course,
                    user=user,
                )
                reservations = list(
                    RtmSTreatmentReservation.objects.select_for_update()
                    .filter(treatment_course=treatment_course)
                    .order_by("sequence_number", "pk")
                )
            snapshot["affected_sessions_after"] = [
                _session_snapshot(item)
                for item in TreatmentSession.objects.select_for_update()
                .filter(**scope, session_date__gte=treatment_date)
                .order_by("session_date", "pk")
            ]
            snapshot["reservations_after"] = _reservation_snapshot(reservations)
            snapshot["discharge_after"] = _snapshot_discharge(patient, treatment_course)
            root_skip = TreatmentSkip.objects.create(
                treatment=selected_session,
                action_type="cancel",
                batch_id=batch_id,
                effective_date=treatment_date,
                reason=reason,
                performed_by=user,
                snapshot=snapshot,
            )
            batch_records = [{
                "id": root_skip.pk,
                "treatment_session_id": selected_session.pk,
                "batch_role": "root",
                "effective_date": treatment_date.isoformat(),
                "reason": reason,
                "performed_by_id": user.pk if user else None,
            }]
            for target_session in target_sessions:
                if target_session.pk == selected_session.pk:
                    continue
                member_snapshot = {
                    "version": SNAPSHOT_VERSION,
                    "kind": "cancel",
                    "batch_role": "member",
                    "batch_id": str(batch_id),
                    "root_skip_id": root_skip.pk,
                    "patient_id": patient.pk,
                    "treatment_course_id": treatment_course.pk if treatment_course else None,
                    "course_number": writer.course_number,
                    "treatment_session_id": target_session.pk,
                    "effective_date": target_session.session_date.isoformat(),
                    "reason": reason,
                    "performed_by_id": user.pk if user else None,
                }
                member_skip = TreatmentSkip.objects.create(
                    treatment=target_session,
                    action_type="cancel",
                    batch_id=batch_id,
                    effective_date=target_session.session_date,
                    reason=reason,
                    performed_by=user,
                    snapshot=member_snapshot,
                )
                batch_records.append({
                    "id": member_skip.pk,
                    "treatment_session_id": target_session.pk,
                    "batch_role": "member",
                    "effective_date": target_session.session_date.isoformat(),
                    "reason": reason,
                    "performed_by_id": user.pk if user else None,
                })
            snapshot["batch_records"] = batch_records
            root_skip.snapshot = snapshot
            root_skip.save(update_fields=["snapshot"])
            return root_skip

    return _run_with_cancellation_busy_retry(operation)


def _same_session_state(session, expected):
    current = _session_snapshot(session)
    return current == expected


def _same_reservation_state(reservations, expected):
    current = _reservation_snapshot(reservations)
    return current == expected


def _resolve_cancellation_batch(requested_skip, writer):
    if requested_skip.batch_id is None:
        if (
            isinstance(requested_skip.snapshot, dict)
            and requested_skip.snapshot.get("version") != LEGACY_SNAPSHOT_VERSION
        ):
            raise TreatmentCancellationError("取消snapshotの形式が一致しません。")
        return requested_skip, [requested_skip]

    requested_snapshot = requested_skip.snapshot
    if (
        not isinstance(requested_snapshot, dict)
        or requested_snapshot.get("version") != SNAPSHOT_VERSION
        or requested_snapshot.get("kind") != "cancel"
        or requested_snapshot.get("batch_id") != str(requested_skip.batch_id)
        or requested_snapshot.get("batch_role") not in {"root", "member"}
    ):
        raise TreatmentCancellationError("中止バッチ記録の形式が一致しません。")

    root_id = (
        requested_skip.pk
        if requested_snapshot["batch_role"] == "root"
        else requested_snapshot.get("root_skip_id")
    )
    if not isinstance(root_id, int):
        raise TreatmentCancellationError("中止バッチrootが特定できません。")
    root_skip = TreatmentSkip.objects.select_for_update().filter(pk=root_id).select_related(
        "treatment",
    ).first()
    if (
        root_skip is None
        or root_skip.action_type != "cancel"
        or root_skip.batch_id != requested_skip.batch_id
        or root_skip.undone_at is not None
    ):
        raise TreatmentCancellationError("中止バッチrootが一致しません。")

    root_snapshot = root_skip.snapshot
    if (
        not isinstance(root_snapshot, dict)
        or root_snapshot.get("version") != SNAPSHOT_VERSION
        or root_snapshot.get("kind") != "cancel"
        or root_snapshot.get("batch_role") != "root"
        or root_snapshot.get("batch_id") != str(root_skip.batch_id)
        or root_snapshot.get("patient_id") != writer.patient.pk
        or root_snapshot.get("treatment_course_id") != (
            writer.treatment_course.pk if writer.treatment_course else None
        )
        or root_snapshot.get("course_number") != writer.course_number
    ):
        raise TreatmentCancellationError("中止バッチrootの所有関係または形式が一致しません。")
    validate_session_course(
        root_skip.treatment, writer.patient, writer.course_number, writer.treatment_course,
    )

    batch_rows = list(
        TreatmentSkip.objects.select_for_update()
        .filter(batch_id=root_skip.batch_id)
        .select_related("treatment")
        .order_by("pk")
    )
    expected_records = root_snapshot.get("batch_records")
    if (
        not isinstance(expected_records, list)
        or any(not isinstance(item, dict) for item in expected_records)
    ):
        raise TreatmentCancellationError("中止バッチ記録snapshotがありません。")

    current_records = []
    record_session_counts = {}
    root_count = 0
    for batch_row in batch_rows:
        row_snapshot = batch_row.snapshot
        if (
            batch_row.action_type != "cancel"
            or batch_row.undone_at is not None
            or not isinstance(row_snapshot, dict)
            or row_snapshot.get("version") != SNAPSHOT_VERSION
            or row_snapshot.get("kind") != "cancel"
            or row_snapshot.get("batch_id") != str(root_skip.batch_id)
        ):
            raise TreatmentCancellationError("中止バッチ記録が変更されています。")
        validate_session_course(
            batch_row.treatment, writer.patient, writer.course_number, writer.treatment_course,
        )
        role = row_snapshot.get("batch_role")
        if role == "root":
            root_count += 1
            if batch_row.pk != root_skip.pk or batch_row.treatment_id != root_skip.treatment_id:
                raise TreatmentCancellationError("中止バッチrootが一意ではありません。")
        elif role == "member":
            if (
                row_snapshot.get("root_skip_id") != root_skip.pk
                or row_snapshot.get("treatment_session_id") != batch_row.treatment_id
                or row_snapshot.get("patient_id") != writer.patient.pk
                or row_snapshot.get("treatment_course_id") != (
                    writer.treatment_course.pk if writer.treatment_course else None
                )
                or row_snapshot.get("course_number") != writer.course_number
            ):
                raise TreatmentCancellationError("中止バッチmemberの所有関係が一致しません。")
        else:
            raise TreatmentCancellationError("中止バッチroleが不正です。")

        record_session_counts[batch_row.treatment_id] = (
            record_session_counts.get(batch_row.treatment_id, 0) + 1
        )
        current_records.append({
            "id": batch_row.pk,
            "treatment_session_id": batch_row.treatment_id,
            "batch_role": role,
            "effective_date": _date_text(batch_row.effective_date),
            "reason": batch_row.reason,
            "performed_by_id": batch_row.performed_by_id,
        })

    current_records.sort(key=lambda item: item["id"])
    expected_records = sorted(expected_records, key=lambda item: item.get("id", -1))
    target_session_ids = root_snapshot.get("target_session_ids")
    before_records = root_snapshot.get("affected_sessions_before")
    expected_targets = (
        sorted(
            item.get("id") for item in before_records
            if isinstance(item, dict) and item.get("status") == "planned"
        )
        if isinstance(before_records, list)
        else None
    )
    if (
        root_count != 1
        or current_records != expected_records
        or not isinstance(target_session_ids, list)
        or len(set(target_session_ids)) != len(target_session_ids)
        or sorted(target_session_ids) != expected_targets
        or any(record_session_counts.get(session_id) != 1 for session_id in target_session_ids)
        or requested_skip.pk not in {item["id"] for item in expected_records}
        or (
            requested_snapshot["batch_role"] == "member"
            and requested_snapshot.get("root_skip_id") != root_skip.pk
        )
    ):
        raise TreatmentCancellationError("中止バッチの追加・削除が検出されました。")

    return root_skip, batch_rows


def _parse_snapshot_date(value, label, *, allow_none=False):
    if allow_none and value is None:
        return None
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise TreatmentCancellationError(f"{label} snapshotの日付が不正です。") from exc


def _parse_snapshot_datetime(value, label):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise TreatmentCancellationError(f"{label} snapshotの日時が不正です。") from exc


def _restore_reservations_safely(course, before, after, current):
    if not isinstance(before, list) or not isinstance(after, list):
        raise TreatmentCancellationError("Reservation snapshotがありません。")
    if not _same_reservation_state(current, after):
        raise TreatmentCancellationError(
            "Reservationが中止後に変更されています。安全のため取消解除できません。"
        )

    required_keys = {
        "id", "sequence_number", "treatment_date", "status", "capacity_type", "updated_at",
    }
    if any(
        not isinstance(item, dict)
        or not required_keys.issubset(item)
        or not isinstance(item["id"], int)
        or not isinstance(item["sequence_number"], int)
        for item in before + after
    ):
        raise TreatmentCancellationError("Reservation snapshotの形式が不正です。")
    before_by_id = {item["id"]: item for item in before}
    after_by_id = {item["id"]: item for item in after}
    current_by_id = {item.pk: item for item in current}
    if (
        len(before_by_id) != len(before)
        or len(after_by_id) != len(after)
        or set(before_by_id) - set(after_by_id)
        or set(after_by_id) != set(current_by_id)
    ):
        raise TreatmentCancellationError("Reservationの所有関係がsnapshotと一致しません。")
    for rows in (before, after):
        sequences = [item["sequence_number"] for item in rows]
        dates = [item["treatment_date"] for item in rows]
        if (
            len(sequences) != len(set(sequences))
            or any(sequence < 1 or sequence > 30 for sequence in sequences)
            or len(dates) != len(set(dates))
            or any(
                item["status"] not in {"provisional", "confirmed", "cancelled"}
                or item["capacity_type"] not in CAPACITY_TYPES
                for item in rows
            )
        ):
            raise TreatmentCancellationError("Reservation snapshotの内容が不正です。")
        for item in rows:
            _parse_snapshot_date(item["treatment_date"], "Reservation")
            _parse_snapshot_datetime(item["updated_at"], "Reservation updated_at")

    active_targets = [
        item for item in before
        if item["status"] in {"provisional", "confirmed"}
    ]
    if active_targets:
        dates = sorted({date.fromisoformat(item["treatment_date"]) for item in active_targets})
        settings = _settings_for_dates(dates)
        occupancy = _occupancy_for_dates(dates, exclude_course_id=course.pk)
        proposed = {}
        for item in active_targets:
            capacity_type = item["capacity_type"]
            if capacity_type not in CAPACITY_TYPES:
                raise TreatmentCancellationError("Reservationのcapacity種別が不正です。")
            treatment_date = date.fromisoformat(item["treatment_date"])
            key = (treatment_date, capacity_type)
            proposed[key] = proposed.get(key, 0) + 1
        for (treatment_date, capacity_type), count in proposed.items():
            if occupancy.get((treatment_date, capacity_type), 0) + count > settings[treatment_date][capacity_type]:
                raise TreatmentCancellationError(
                    "取消中に治療枠が埋まったためReservationを復元できません。"
                )

    new_reservation_ids = set(after_by_id) - set(before_by_id)
    before_dates = {
        date.fromisoformat(item["treatment_date"])
        for item in before
    }
    new_reservations = [current_by_id[reservation_id] for reservation_id in new_reservation_ids]
    if any(reservation.treatment_date in before_dates for reservation in new_reservations):
        raise TreatmentCancellationError(
            "新規Reservationの日付が取消前Reservationと重なるため、安全に復元できません。"
        )
    for reservation in new_reservations:
        if reservation.status != "cancelled":
            reservation.status = "cancelled"
            reservation.save(update_fields=["status", "updated_at"])
    used_dates = {
        date.fromisoformat(item["treatment_date"])
        for item in before + after
    }
    temporary_dates = []
    temporary_date = date(1901, 1, 1)
    for _reservation_id in before_by_id:
        while temporary_date in used_dates:
            temporary_date += timedelta(days=1)
        temporary_dates.append(temporary_date)
        used_dates.add(temporary_date)
        temporary_date += timedelta(days=1)
    for index, reservation_id in enumerate(before_by_id):
        reservation = current_by_id[reservation_id]
        reservation.treatment_date = temporary_dates[index]
        reservation.save(update_fields=["treatment_date", "updated_at"])
    for reservation_id, original in before_by_id.items():
        reservation = current_by_id[reservation_id]
        reservation.treatment_date = date.fromisoformat(original["treatment_date"])
        reservation.sequence_number = original["sequence_number"]
        reservation.status = original["status"]
        reservation.capacity_type = original["capacity_type"]
        reservation.save(update_fields=[
            "treatment_date", "sequence_number", "status", "capacity_type", "updated_at",
        ])


def undo_treatment_cancellation(*, skip_id, user):
    """Restore a cancellation only when every affected row still matches its snapshot."""
    initial_skip = TreatmentSkip.objects.select_related("treatment").get(pk=skip_id)
    initial_session = initial_skip.treatment

    def operation():
        with patient_course_writer(
            initial_session.patient_id,
            initial_session.course_number,
            expected_course_id=initial_session.treatment_course_id,
        ) as writer:
            requested_skip = TreatmentSkip.objects.select_for_update().get(pk=skip_id)
            if requested_skip.action_type != "cancel" or requested_skip.undone_at is not None:
                raise TreatmentCancellationError("取消記録が未処理状態ではありません。")
            skip, batch_rows = _resolve_cancellation_batch(requested_skip, writer)
            if skip.undone_at is not None:
                raise TreatmentCancellationError("取消記録が未処理状態ではありません。")
            session = TreatmentSession.objects.select_for_update().filter(
                pk=skip.treatment_id,
            ).first()
            if session is None:
                raise TreatmentCancellationError("中止対象Sessionが見つかりません。")
            validate_session_course(
                session, writer.patient, writer.course_number, writer.treatment_course,
            )
            snapshot = skip.snapshot
            if (
                not isinstance(snapshot, dict)
                or snapshot.get("version") != (
                    SNAPSHOT_VERSION if skip.batch_id is not None else LEGACY_SNAPSHOT_VERSION
                )
                or snapshot.get("kind") != "cancel"
                or snapshot.get("patient_id") != writer.patient.pk
                or snapshot.get("treatment_course_id") != (
                    writer.treatment_course.pk if writer.treatment_course else None
                )
                or snapshot.get("course_number") != writer.course_number
                or snapshot.get("selected_session_id") != session.pk
                or (
                    skip.batch_id is not None
                    and (
                        snapshot.get("batch_role") != "root"
                        or snapshot.get("batch_id") != str(skip.batch_id)
                    )
                )
            ):
                raise TreatmentCancellationError("取消snapshotの所有関係または形式が一致しません。")

            effective_date = _parse_snapshot_date(
                snapshot.get("effective_date"), "中止対象",
            )
            if skip.effective_date != effective_date:
                raise TreatmentCancellationError("中止対象日とsnapshotが一致しません。")
            sessions = list(
                TreatmentSession.objects.select_for_update()
                .filter(**writer.session_filter, session_date__gte=effective_date)
                .order_by("session_date", "pk")
            )
            expected_after = snapshot.get("affected_sessions_after")
            if not isinstance(expected_after, list) or len(sessions) != len(expected_after):
                raise TreatmentCancellationError(
                    "Session件数が中止後snapshotと一致しません。取消解除できません。"
                )
            if any(not isinstance(item, dict) for item in expected_after):
                raise TreatmentCancellationError("中止後Session snapshotの形式が不正です。")
            current_by_id = {item.pk: item for item in sessions}
            expected_by_id = {item.get("id"): item for item in expected_after}
            if (
                len(expected_by_id) != len(expected_after)
                or None in expected_by_id
                or set(current_by_id) != set(expected_by_id)
            ):
                raise TreatmentCancellationError(
                    "Sessionの追加・削除が検出されたため取消解除できません。"
                )
            for session_id, expected in expected_by_id.items():
                current = current_by_id[session_id]
                validate_session_course(
                    current, writer.patient, writer.course_number, writer.treatment_course,
                )
                if not _same_session_state(current, expected):
                    raise TreatmentCancellationError(
                        "Sessionが中止後に変更されています。安全のため取消解除できません。"
                    )

            before_discharge = snapshot.get("discharge_before")
            after_discharge = snapshot.get("discharge_after")
            if not isinstance(before_discharge, dict) or not isinstance(after_discharge, dict):
                raise TreatmentCancellationError("退院日snapshotがありません。")
            discharge_keys = {"course_discharge_date", "patient_discharge_date"}
            if not discharge_keys.issubset(before_discharge) or not discharge_keys.issubset(after_discharge):
                raise TreatmentCancellationError("退院日snapshotの形式が不正です。")
            if _snapshot_discharge(writer.patient, writer.treatment_course) != after_discharge:
                raise TreatmentCancellationError(
                    "退院日が中止後に変更されています。安全のため取消解除できません。"
                )

            reservations = []
            if writer.treatment_course is not None:
                reservations = list(
                    RtmSTreatmentReservation.objects.select_for_update()
                    .filter(treatment_course=writer.treatment_course)
                    .order_by("sequence_number", "pk")
                )
                _restore_reservations_safely(
                    writer.treatment_course,
                    snapshot.get("reservations_before"),
                    snapshot.get("reservations_after"),
                    reservations,
                )
            else:
                if (
                    snapshot.get("reservations_before") != []
                    or snapshot.get("reservations_after") != []
                ):
                    raise TreatmentCancellationError("CourseなしSessionのReservation snapshotが不正です。")

            before_sessions = snapshot.get("affected_sessions_before")
            if not isinstance(before_sessions, list) or len(before_sessions) != len(sessions):
                raise TreatmentCancellationError("取消前Session snapshotが不正です。")
            if any(not isinstance(item, dict) for item in before_sessions):
                raise TreatmentCancellationError("取消前Session snapshotの形式が不正です。")
            before_by_id = {item.get("id"): item for item in before_sessions}
            if (
                len(before_by_id) != len(before_sessions)
                or set(before_by_id) != set(current_by_id)
            ):
                raise TreatmentCancellationError("取消前後のSession対応が一致しません。")
            after_by_id = expected_by_id
            for session_id, original in before_by_id.items():
                expected_status = (
                    "skipped" if original.get("status") == "planned"
                    else original.get("status")
                )
                expected = dict(original)
                expected["status"] = expected_status
                expected_fields = dict(original.get("fields") or {})
                expected_fields["status"] = expected_status
                expected["fields"] = expected_fields
                if (
                    original.get("status") not in {"planned", "done", "skipped"}
                    or expected != after_by_id.get(session_id)
                ):
                    raise TreatmentCancellationError(
                        "取消前後のSession snapshotが整合しません。"
                    )
            selected_before = before_by_id.get(session.pk)
            if (
                selected_before is None
                or selected_before.get("status") not in {"planned", "done", "skipped"}
                or selected_before.get("session_date") != effective_date.isoformat()
                or skip.effective_date != effective_date
            ):
                raise TreatmentCancellationError("中止対象とsnapshotの対応が一致しません。")
            original_course_date = _parse_snapshot_date(
                before_discharge["course_discharge_date"],
                "Course退院日",
                allow_none=True,
            )
            original_patient_date = _parse_snapshot_date(
                before_discharge["patient_discharge_date"],
                "Patient退院日",
                allow_none=True,
            )
            for session_id, original in before_by_id.items():
                current = current_by_id[session_id]
                if (
                    original.get("patient_id") != writer.patient.pk
                    or original.get("treatment_course_id") != (
                        writer.treatment_course.pk if writer.treatment_course else None
                    )
                    or original.get("course_number") != writer.course_number
                ):
                    raise TreatmentCancellationError("取消前Session snapshotの所有関係が不正です。")
                current.status = original["status"]
                current.save(update_fields=["status"])

            if writer.treatment_course is not None:
                writer.treatment_course.discharge_date = (
                    original_course_date
                )
                writer.treatment_course.save(update_fields=["discharge_date"])
                if writer.course_number == 1:
                    writer.patient.discharge_date = (
                        original_patient_date
                    )
                    writer.patient.save(update_fields=["discharge_date"])
            else:
                writer.patient.discharge_date = (
                    original_patient_date
                )
                writer.patient.save(update_fields=["discharge_date"])

            skip.undone_by = user
            from django.utils import timezone
            undone_at = timezone.now()
            for batch_row in batch_rows:
                batch_row.undone_by = user
                batch_row.undone_at = undone_at
                batch_row.save(update_fields=["undone_by", "undone_at"])
            return skip

    return _run_with_cancellation_busy_retry(operation)
