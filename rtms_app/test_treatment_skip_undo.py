import sqlite3
from datetime import date, timedelta
from threading import Event, Thread
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import IntegrityError, OperationalError, connection, connections, transaction
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from rtms_app.models import (
    MappingSchedule,
    Patient,
    RtmSTreatmentCapacity,
    RtmSTreatmentReservation,
    TreatmentCourse,
    TreatmentSession,
    TreatmentSkip,
)
from rtms_app.services import treatment_skip_undo
from rtms_app.services.rtms_schedule import (
    generate_treatment_dates,
    get_japanese_holidays,
)
from rtms_app.services.rtms_treatment_reservations import (
    InvalidTreatmentReservationError,
    sync_rtms_treatment_reservation_from_sessions,
)
from rtms_app.services.session_writes import PatientCourseWriteError
from rtms_app.services.patient_mutex import acquire_patient_mutex
from rtms_app.services.treatment_skip_undo import (
    TreatmentSkipUndoError,
    V2_SNAPSHOT_MARKER_KEYS,
)


class TreatmentSkipUndoSafetyTests(TestCase):
    start_date = date(2026, 10, 5)

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="skip-undo-safety",
            password="pw",
        )
        self.client.force_login(self.user)
        self.patient = Patient.objects.create(
            card_id="SKIPUNDO1",
            name="Skip Undo Safety",
            birth_date=date(1980, 1, 1),
        )

    def _make_course(self, course_number=1, count=4, start_date=None):
        start_date = start_date or self.start_date
        course = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=course_number,
            admission_date=start_date,
            first_treatment_date=start_date,
        )
        holidays = get_japanese_holidays(
            start_date,
            start_date + timedelta(days=365),
        )
        dates = generate_treatment_dates(
            start_date,
            total=count,
            holidays=holidays,
        )
        sessions = [
            TreatmentSession.objects.create(
                patient=self.patient,
                treatment_course=course,
                course_number=course_number,
                session_date=treatment_date,
                status="planned",
            )
            for treatment_date in dates
        ]
        return course, sessions

    def _make_legacy_sessions(self, count=3):
        holidays = get_japanese_holidays(
            self.start_date,
            self.start_date + timedelta(days=365),
        )
        dates = generate_treatment_dates(
            self.start_date,
            total=count,
            holidays=holidays,
        )
        return [
            TreatmentSession.objects.create(
                patient=self.patient,
                course_number=1,
                session_date=treatment_date,
                status="planned",
            )
            for treatment_date in dates
        ]

    def _post_skip(self, selected, course_number=None):
        data = {
            "treatment_date": selected.session_date.isoformat(),
            "treatment_time": "09:00",
            "mt_percent": "120",
            "frequency_hz": "18.0",
            "train_seconds": "2.0",
            "intertrain_seconds": "20.0",
            "train_count": "55",
            "total_pulses": "1980",
            "action": "skip",
        }
        if course_number is not None:
            data["course_number"] = str(course_number)
        response = self.client.post(
            reverse("rtms_app:treatment_add", args=[self.patient.pk]),
            data,
        )
        self.assertEqual(response.status_code, 302, response.content.decode())
        return TreatmentSkip.objects.get(treatment_id=selected.pk, action_type="postpone")

    def _undo(self, skip):
        return self.client.post(
            reverse("rtms_app:treatment_skip_undo", args=[skip.pk]),
        )

    def _state(self, course, skip):
        if course is None:
            session_scope = {
                "patient": self.patient,
                "course_number": 1,
                "treatment_course__isnull": True,
            }
            mapping_scope = {
                "patient": self.patient,
                "course_number": 1,
                "treatment_course__isnull": True,
            }
            reservations = RtmSTreatmentReservation.objects.none()
        else:
            session_scope = {"treatment_course": course}
            mapping_scope = {"treatment_course": course}
            reservations = RtmSTreatmentReservation.objects.filter(
                treatment_course=course,
            )
        return {
            "sessions": list(
                TreatmentSession.objects.filter(**session_scope)
                .order_by("pk")
                .values_list(
                    "pk", "patient_id", "treatment_course_id", "course_number",
                    "session_date", "date", "slot", "status", "treatment_notes",
                )
            ),
            "reservations": list(
                reservations
                .order_by("pk")
                .values_list(
                    "pk", "treatment_course_id", "treatment_date", "sequence_number",
                    "status", "capacity_type", "created_at", "updated_at",
                )
            ),
            "mapping_schedules": list(
                MappingSchedule.objects.filter(**mapping_scope)
                .order_by("pk")
                .values_list(
                    "pk", "patient_id", "treatment_course_id", "course_number",
                    "week_number", "planned_date", "updated_at",
                )
            ),
            "skip": TreatmentSkip.objects.filter(pk=skip.pk).values_list(
                "treatment_id", "action_type", "effective_date", "reason",
                "performed_by_id", "batch_id", "snapshot", "undone_by_id", "undone_at",
            ).get(),
        }

    def _course_state(self, course):
        return {
            "sessions": list(
                TreatmentSession.objects.filter(treatment_course=course)
                .order_by("pk")
                .values_list(
                    "pk", "patient_id", "treatment_course_id", "course_number",
                    "session_date", "date", "slot", "status", "treatment_notes",
                )
            ),
            "reservations": list(
                RtmSTreatmentReservation.objects.filter(treatment_course=course)
                .order_by("pk")
                .values_list(
                    "pk", "treatment_course_id", "sequence_number", "treatment_date",
                    "status", "capacity_type", "created_at", "updated_at",
                )
            ),
            "mapping_schedules": list(
                MappingSchedule.objects.filter(treatment_course=course)
                .order_by("pk")
                .values_list(
                    "pk", "patient_id", "treatment_course_id", "course_number",
                    "week_number", "planned_date", "updated_at",
                )
            ),
        }

    def _assert_undo_rejected_without_changes(self, course, skip):
        state_before = self._state(course, skip)
        response = self._undo(skip)
        self.assertEqual(response.status_code, 400, response.content.decode())
        self.assertEqual(self._state(course, skip), state_before)

    def _strip_v2_snapshot_fields(self, skip):
        snapshot = dict(skip.snapshot)
        snapshot.pop("undo_snapshot_version", None)
        for key in V2_SNAPSHOT_MARKER_KEYS:
            snapshot.pop(key, None)
        skip.snapshot = snapshot
        skip.save(update_fields=["snapshot"])

    def _make_reservation_course(self):
        course, sessions = self._make_course(count=30)
        sync_rtms_treatment_reservation_from_sessions(treatment_course=course)
        return course, sessions

    def test_course_skip_undo_restores_sessions_and_mapping_snapshot(self):
        course, sessions = self._make_course()
        sessions_before = list(
            TreatmentSession.objects.filter(treatment_course=course)
            .order_by("pk")
            .values_list("pk", "session_date", "status")
        )
        self.assertFalse(MappingSchedule.objects.filter(treatment_course=course).exists())

        skip = self._post_skip(sessions[0], course_number=1)
        self.assertEqual(skip.snapshot["undo_snapshot_version"], 2)
        self.assertTrue(MappingSchedule.objects.filter(treatment_course=course).exists())

        response = self._undo(skip)

        self.assertEqual(response.status_code, 302, response.content.decode())
        self.assertEqual(
            list(TreatmentSession.objects.filter(treatment_course=course)
                 .order_by("pk").values_list("pk", "session_date", "status")),
            sessions_before,
        )
        self.assertFalse(MappingSchedule.objects.filter(treatment_course=course).exists())
        skip.refresh_from_db()
        self.assertEqual(skip.undone_by_id, self.user.pk)
        self.assertIsNotNone(skip.undone_at)

    def test_existing_mapping_schedule_date_is_restored(self):
        course, sessions = self._make_course()
        mapping = MappingSchedule.objects.create(
            patient=self.patient,
            treatment_course=course,
            course_number=1,
            week_number=1,
            planned_date=self.start_date + timedelta(days=2),
        )
        skip = self._post_skip(sessions[0], course_number=1)
        mapping.refresh_from_db()
        self.assertEqual(mapping.planned_date, self.start_date)

        response = self._undo(skip)

        self.assertEqual(response.status_code, 302, response.content.decode())
        mapping.refresh_from_db()
        self.assertEqual(mapping.planned_date, self.start_date + timedelta(days=2))

    def test_existing_reservations_keep_ids_sequence_dates_and_status_after_undo(self):
        course, sessions = self._make_reservation_course()
        before = list(
            RtmSTreatmentReservation.objects.filter(treatment_course=course)
            .order_by("sequence_number")
            .values_list("pk", "treatment_date", "sequence_number", "status")
        )
        skip = self._post_skip(sessions[0], course_number=1)

        response = self._undo(skip)

        self.assertEqual(response.status_code, 302, response.content.decode())
        self.assertEqual(
            list(RtmSTreatmentReservation.objects.filter(treatment_course=course)
                 .order_by("sequence_number")
                 .values_list("pk", "treatment_date", "sequence_number", "status")),
            before,
        )

    def test_course_two_undo_does_not_change_course_one(self):
        course_one, course_one_sessions = self._make_course(course_number=1)
        course_two, course_two_sessions = self._make_course(course_number=2)
        RtmSTreatmentReservation.objects.create(
            treatment_course=course_one,
            treatment_date=course_one_sessions[0].session_date,
            sequence_number=1,
            status="cancelled",
            capacity_type="rTMS_private",
        )
        MappingSchedule.objects.create(
            patient=self.patient,
            treatment_course=course_one,
            course_number=1,
            week_number=1,
            planned_date=course_one_sessions[0].session_date,
        )
        skip = self._post_skip(course_two_sessions[0], course_number=2)
        course_one_before = self._course_state(course_one)

        response = self._undo(skip)

        self.assertEqual(response.status_code, 302, response.content.decode())
        self.assertEqual(self._course_state(course_one), course_one_before)
        self.assertEqual(
            list(TreatmentSession.objects.filter(treatment_course=course_two)
                 .order_by("pk").values_list("status", flat=True)),
            ["planned"] * len(course_two_sessions),
        )

    def test_legacy_snapshot_without_new_metadata_remains_undoable_when_verifiable(self):
        sessions = self._make_legacy_sessions()
        skip = self._post_skip(sessions[0])
        snapshot = dict(skip.snapshot)
        for key in V2_SNAPSHOT_MARKER_KEYS | {
            "undo_snapshot_version",
            "skipped_session_id",
            "skipped_session_date",
        }:
            snapshot.pop(key, None)
        skip.snapshot = snapshot
        skip.save(update_fields=["snapshot"])

        response = self._undo(skip)

        self.assertEqual(response.status_code, 302, response.content.decode())
        for session in sessions:
            session.refresh_from_db()
            self.assertEqual(session.status, "planned")
        self.assertEqual(
            [session.session_date for session in sessions],
            list(TreatmentSession.objects.filter(patient=self.patient)
                 .order_by("pk").values_list("session_date", flat=True)),
        )

    def test_v2_snapshot_missing_only_version_is_rejected_without_changes(self):
        sessions = self._make_legacy_sessions()
        skip = self._post_skip(sessions[0])
        skip.snapshot.pop("undo_snapshot_version")
        skip.save(update_fields=["snapshot"])

        self._assert_undo_rejected_without_changes(None, skip)

    def test_versionless_snapshot_with_partial_v2_markers_is_rejected(self):
        sessions = self._make_legacy_sessions()
        skip = self._post_skip(sessions[0])
        snapshot = dict(skip.snapshot)
        snapshot.pop("undo_snapshot_version")
        for key in V2_SNAPSHOT_MARKER_KEYS - {"patient_id", "all_sessions_after"}:
            snapshot.pop(key, None)
        skip.snapshot = snapshot
        skip.save(update_fields=["snapshot"])

        self._assert_undo_rejected_without_changes(None, skip)

    def test_legacy_snapshot_with_reservations_is_rejected_without_changes(self):
        course, sessions = self._make_reservation_course()
        skip = self._post_skip(sessions[0], course_number=1)
        self._strip_v2_snapshot_fields(skip)

        self._assert_undo_rejected_without_changes(course, skip)

    def test_legacy_snapshot_with_mapping_schedules_is_rejected_without_changes(self):
        course, sessions = self._make_course()
        skip = self._post_skip(sessions[0], course_number=1)
        self._strip_v2_snapshot_fields(skip)

        self._assert_undo_rejected_without_changes(course, skip)

    def test_wrong_patient_snapshot_is_rejected_without_changes(self):
        course, sessions = self._make_course()
        skip = self._post_skip(sessions[0], course_number=1)
        skip.snapshot["patient_id"] = self.patient.pk + 1
        skip.save(update_fields=["snapshot"])

        self._assert_undo_rejected_without_changes(course, skip)

    def test_wrong_course_snapshot_is_rejected_without_changes(self):
        course, sessions = self._make_course()
        skip = self._post_skip(sessions[0], course_number=1)
        skip.snapshot["course_number"] = 2
        skip.save(update_fields=["snapshot"])

        self._assert_undo_rejected_without_changes(course, skip)

    def test_skip_repointed_to_another_session_is_rejected_without_changes(self):
        course, sessions = self._make_course()
        skip = self._post_skip(sessions[0], course_number=1)
        skip.treatment = sessions[1]
        skip.save(update_fields=["treatment"])

        self._assert_undo_rejected_without_changes(course, skip)

    def test_missing_or_malformed_snapshot_is_rejected_without_changes(self):
        course, sessions = self._make_course()
        skip = self._post_skip(sessions[0], course_number=1)
        skip.snapshot["all_sessions_after"] = None
        skip.save(update_fields=["snapshot"])

        self._assert_undo_rejected_without_changes(course, skip)

    def test_inconsistent_affected_session_snapshot_is_rejected(self):
        course, sessions = self._make_course()
        skip = self._post_skip(sessions[0], course_number=1)
        skip.snapshot["affected_sessions_before"][0]["fields"]["treatment_notes"] = (
            "tampered snapshot"
        )
        skip.save(update_fields=["snapshot"])

        self._assert_undo_rejected_without_changes(course, skip)

    def test_second_undo_post_is_rejected(self):
        course, sessions = self._make_course()
        skip = self._post_skip(sessions[0], course_number=1)

        first = self._undo(skip)
        skip.refresh_from_db()
        state_after_first = self._state(course, skip)
        second = self._undo(skip)

        self.assertEqual(first.status_code, 302, first.content.decode())
        self.assertEqual(second.status_code, 400, second.content.decode())
        self.assertEqual(self._state(course, skip), state_after_first)

    def test_changed_session_after_skip_is_rejected_without_changes(self):
        course, sessions = self._make_course()
        skip = self._post_skip(sessions[0], course_number=1)
        future = TreatmentSession.objects.get(pk=sessions[1].pk)
        future.treatment_notes = "changed after skip"
        future.save(update_fields=["treatment_notes"])

        self._assert_undo_rejected_without_changes(course, skip)

    def test_changed_reservation_after_skip_is_rejected_without_changes(self):
        course, sessions = self._make_reservation_course()
        skip = self._post_skip(sessions[0], course_number=1)
        reservation = RtmSTreatmentReservation.objects.filter(
            treatment_course=course,
            status="provisional",
        ).first()
        reservation.status = "confirmed"
        reservation.save(update_fields=["status", "updated_at"])

        self._assert_undo_rejected_without_changes(course, skip)

    def test_new_done_session_at_restore_date_is_rejected_without_changes(self):
        for status, course_number in (("done", 1), ("skipped", 2)):
            with self.subTest(status=status):
                course, sessions = self._make_course(
                    course_number=course_number,
                    count=2,
                )
                sessions[1].session_date = sessions[0].session_date + timedelta(days=2)
                sessions[1].save(update_fields=["session_date"])
                original_future_date = sessions[1].session_date
                skip = self._post_skip(sessions[0], course_number=course_number)
                fixed = TreatmentSession.objects.create(
                    patient=self.patient,
                    treatment_course=course,
                    course_number=course_number,
                    session_date=original_future_date,
                    status=status,
                )
                state_before = self._state(course, skip)

                response = self._undo(skip)

                self.assertEqual(response.status_code, 400, response.content.decode())
                self.assertEqual(self._state(course, skip), state_before)
                fixed.refresh_from_db()
                self.assertEqual(fixed.status, status)

    def test_capacity_shortage_rolls_back_sessions_reservations_mapping_and_skip(self):
        course, sessions = self._make_reservation_course()
        skip = self._post_skip(sessions[0], course_number=1)
        capacities = list(RtmSTreatmentCapacity.objects.filter(
            capacity_type__in=("rTMS_private", "general_ward"),
            valid_from=date(2026, 1, 1),
        ))
        self.assertEqual(len(capacities), 2)
        for capacity in capacities:
            capacity.capacity = 0
            capacity.save(update_fields=["capacity", "updated_at"])
        state_before = self._state(course, skip)

        response = self._undo(skip)

        self.assertEqual(response.status_code, 400, response.content.decode())
        self.assertEqual(self._state(course, skip), state_before)

    def test_reservation_unique_conflict_rolls_back_entire_undo(self):
        course, sessions = self._make_reservation_course()
        skip = self._post_skip(sessions[0], course_number=1)
        state_before = self._state(course, skip)
        sync_reservations = treatment_skip_undo.sync_rtms_treatment_reservation_from_sessions

        def sync_then_duplicate_date(**kwargs):
            sync_reservations(**kwargs)
            reservations = list(
                RtmSTreatmentReservation.objects.filter(treatment_course=course)
                .order_by("sequence_number")[:2]
            )
            reservations[0].treatment_date = reservations[1].treatment_date
            reservations[0].save(update_fields=["treatment_date", "updated_at"])

        with patch(
            "rtms_app.services.treatment_skip_undo.sync_rtms_treatment_reservation_from_sessions",
            side_effect=sync_then_duplicate_date,
        ):
            response = self._undo(skip)

        self.assertEqual(response.status_code, 400, response.content.decode())
        self.assertEqual(self._state(course, skip), state_before)

    def test_mapping_schedule_restore_failure_rolls_back_everything(self):
        course, sessions = self._make_course()
        skip = self._post_skip(sessions[0], course_number=1)
        state_before = self._state(course, skip)
        restore_mapping = treatment_skip_undo._restore_mapping_schedules

        def restore_then_fail(*args):
            restore_mapping(*args)
            raise TreatmentSkipUndoError("forced mapping restore failure")

        with patch(
            "rtms_app.services.treatment_skip_undo._restore_mapping_schedules",
            side_effect=restore_then_fail,
        ):
            response = self._undo(skip)

        self.assertEqual(response.status_code, 400, response.content.decode())
        self.assertEqual(self._state(course, skip), state_before)

    def test_session_restore_failure_rolls_back_everything(self):
        course, sessions = self._make_reservation_course()
        skip = self._post_skip(sessions[0], course_number=1)
        state_before = self._state(course, skip)
        restore_sessions = treatment_skip_undo._restore_sessions

        def restore_then_fail(*args):
            restore_sessions(*args)
            raise TreatmentSkipUndoError("forced session restore failure")

        with patch(
            "rtms_app.services.treatment_skip_undo._restore_sessions",
            side_effect=restore_then_fail,
        ):
            response = self._undo(skip)

        self.assertEqual(response.status_code, 400, response.content.decode())
        self.assertEqual(self._state(course, skip), state_before)

    def test_reservation_sync_error_rolls_back_everything(self):
        course, sessions = self._make_reservation_course()
        skip = self._post_skip(sessions[0], course_number=1)
        state_before = self._state(course, skip)
        sync_reservations = treatment_skip_undo.sync_rtms_treatment_reservation_from_sessions

        def sync_then_fail(**kwargs):
            sync_reservations(**kwargs)
            raise InvalidTreatmentReservationError("forced reservation sync failure")

        with patch(
            "rtms_app.services.treatment_skip_undo.sync_rtms_treatment_reservation_from_sessions",
            side_effect=sync_then_fail,
        ):
            response = self._undo(skip)

        self.assertEqual(response.status_code, 400, response.content.decode())
        self.assertEqual(self._state(course, skip), state_before)

    def test_sqlite_busy_retry_restarts_whole_undo_and_reacquires_mutex(self):
        course, sessions = self._make_course()
        skip = self._post_skip(sessions[0], course_number=1)
        restore_sessions = treatment_skip_undo._restore_sessions
        attempts = 0

        def fail_once_after_restore(*args):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                restore_sessions(*args)
                error = OperationalError("database is locked")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise error
            self.assertEqual(
                TreatmentSession.objects.get(pk=sessions[0].pk).status,
                "skipped",
            )
            return restore_sessions(*args)

        from rtms_app.services import session_writes
        acquire_mutex = session_writes.acquire_patient_mutex
        with patch(
            "rtms_app.services.treatment_skip_undo._restore_sessions",
            side_effect=fail_once_after_restore,
        ), patch.object(
            session_writes,
            "acquire_patient_mutex",
            wraps=acquire_mutex,
        ) as acquire_patient:
            response = self._undo(skip)

        self.assertEqual(response.status_code, 302, response.content.decode())
        self.assertEqual(attempts, 2)
        self.assertEqual(acquire_patient.call_count, 2)
        skip.refresh_from_db()
        self.assertIsNotNone(skip.undone_at)

    def test_non_busy_operational_error_is_not_retried_and_rolls_back(self):
        course, sessions = self._make_course()
        skip = self._post_skip(sessions[0], course_number=1)
        state_before = self._state(course, skip)
        restore_sessions = treatment_skip_undo._restore_sessions
        attempts = 0

        def restore_then_fail(*args):
            nonlocal attempts
            attempts += 1
            restore_sessions(*args)
            raise OperationalError("disk I/O error")

        with patch(
            "rtms_app.services.treatment_skip_undo._restore_sessions",
            side_effect=restore_then_fail,
        ):
            with self.assertRaises(OperationalError):
                treatment_skip_undo.undo_treatment_skip(
                    skip_id=skip.pk,
                    patient_id=self.patient.pk,
                    course_number=1,
                    treatment_course_id=course.pk,
                    treatment_id=sessions[0].pk,
                    user=self.user,
                )

        self.assertEqual(attempts, 1)
        self.assertEqual(self._state(course, skip), state_before)

    def test_outer_transaction_savepoint_rolls_back_undo_and_mutex_is_first_data_sql(self):
        course, sessions = self._make_course()
        skip = self._post_skip(sessions[0], course_number=1)
        state_before = self._state(course, skip)
        restore_sessions = treatment_skip_undo._restore_sessions

        def restore_then_fail(*args):
            restore_sessions(*args)
            raise TreatmentSkipUndoError("forced failure inside outer transaction")

        with patch(
            "rtms_app.services.treatment_skip_undo._restore_sessions",
            side_effect=restore_then_fail,
        ), CaptureQueriesContext(connection) as captured:
            with self.assertRaises(TreatmentSkipUndoError):
                treatment_skip_undo.undo_treatment_skip(
                    skip_id=skip.pk,
                    patient_id=self.patient.pk,
                    course_number=1,
                    treatment_course_id=course.pk,
                    treatment_id=sessions[0].pk,
                    user=self.user,
                )

        sql = [query["sql"].strip().upper() for query in captured.captured_queries]
        self.assertTrue(sql[0].startswith("SAVEPOINT"), sql)
        data_sql = [
            statement for statement in sql
            if statement.startswith(("SELECT", "UPDATE", "INSERT", "DELETE"))
        ]
        self.assertTrue(data_sql[0].startswith("UPDATE"), data_sql)
        self.assertIn("RTMS_APP_PATIENT", data_sql[0])
        self.assertEqual(self._state(course, skip), state_before)


class TreatmentSkipUndoSqliteContentionTests(TransactionTestCase):
    reset_sequences = True

    def test_real_sqlite_patient_lock_retries_from_mutex(self):
        user = get_user_model().objects.create_user(
            username="skip-undo-sqlite-lock",
            password="pw",
        )
        patient = Patient.objects.create(
            card_id="SKIPUNDOLOCK",
            name="Skip Undo SQLite Lock",
            birth_date=date(1980, 1, 1),
        )
        course = TreatmentCourse.objects.create(
            patient=patient,
            course_number=1,
            admission_date=date(2026, 10, 5),
            first_treatment_date=date(2026, 10, 5),
        )
        sessions = [
            TreatmentSession.objects.create(
                patient=patient,
                treatment_course=course,
                course_number=1,
                session_date=date(2026, 10, 5) + timedelta(days=offset),
                status="planned",
            )
            for offset in range(2)
        ]
        client = self.client
        client.force_login(user)
        response = client.post(
            reverse("rtms_app:treatment_add", args=[patient.pk]),
            {
                "treatment_date": sessions[0].session_date.isoformat(),
                "treatment_time": "09:00",
                "mt_percent": "120",
                "frequency_hz": "18.0",
                "train_seconds": "2.0",
                "intertrain_seconds": "20.0",
                "train_count": "55",
                "total_pulses": "1980",
                "action": "skip",
                "course_number": "1",
            },
        )
        self.assertEqual(response.status_code, 302, response.content.decode())
        skip = TreatmentSkip.objects.get(treatment_id=sessions[0].pk)

        lock_acquired = Event()
        release_lock = Event()
        lock_released = Event()
        holder_errors = []

        def hold_patient_lock():
            connections.close_all()
            try:
                with transaction.atomic():
                    acquire_patient_mutex(patient.pk)
                    lock_acquired.set()
                    if not release_lock.wait(timeout=10):
                        raise TimeoutError("test did not release the SQLite writer lock")
            except BaseException as exc:
                holder_errors.append(exc)
            finally:
                lock_released.set()
                connections.close_all()

        holder = Thread(target=hold_patient_lock, daemon=True)
        holder.start()
        try:
            self.assertTrue(lock_acquired.wait(timeout=5))
            with connection.cursor() as cursor:
                cursor.execute("PRAGMA busy_timeout = 50")

            def release_after_busy(_delay):
                release_lock.set()
                if not lock_released.wait(timeout=5):
                    raise TimeoutError("SQLite writer lock was not released")

            from rtms_app.services import session_writes
            acquire_mutex = session_writes.acquire_patient_mutex
            with patch(
                "rtms_app.services.treatment_cancellation.time.sleep",
                side_effect=release_after_busy,
            ) as retry_sleep, patch.object(
                session_writes,
                "acquire_patient_mutex",
                wraps=acquire_mutex,
            ) as acquire_patient, CaptureQueriesContext(connection) as captured:
                with transaction.atomic():
                    patient_id = treatment_skip_undo.undo_treatment_skip(
                        skip_id=skip.pk,
                        patient_id=patient.pk,
                        course_number=1,
                        treatment_course_id=course.pk,
                        treatment_id=sessions[0].pk,
                        user=user,
                    )

            self.assertEqual(patient_id, patient.pk)
            self.assertGreaterEqual(retry_sleep.call_count, 1)
            self.assertEqual(acquire_patient.call_count, retry_sleep.call_count + 1)
            sql = [query["sql"].strip().upper() for query in captured.captured_queries]
            data_sql = [
                statement for statement in sql
                if statement.startswith(("SELECT", "UPDATE", "INSERT", "DELETE"))
            ]
            self.assertTrue(data_sql[0].startswith("UPDATE"), data_sql)
            self.assertIn("RTMS_APP_PATIENT", data_sql[0])
            self.assertEqual(holder_errors, [])
        finally:
            release_lock.set()
            holder.join(timeout=5)
            connections.close_all()
        self.assertFalse(holder.is_alive())


class TreatmentSkipPostCommitAuditTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="skip-audit-operator",
            password="pw",
        )
        self.client.force_login(self.user)
        self.patient = Patient.objects.create(
            card_id="SKIPAUDIT1",
            name="Skip Audit Patient",
            birth_date=date(1980, 1, 1),
        )
        self.session = TreatmentSession.objects.create(
            patient=self.patient,
            session_date=date(2026, 10, 5),
            status="planned",
        )

    def test_audit_callback_failure_after_commit_is_logged_without_false_failure(self):
        post_data = {
            "treatment_date": self.session.session_date.isoformat(),
            "treatment_time": "09:00",
            "mt_percent": "120",
            "frequency_hz": "18.0",
            "train_seconds": "2.0",
            "intertrain_seconds": "20.0",
            "train_count": "55",
            "total_pulses": "1980",
            "side_effect_rows_json": "[]",
            "sae_selection_changed": "",
            "action": "skip",
        }

        with self.assertLogs("django.db.backends.base", level="ERROR") as captured:
            with patch(
                "rtms_app.signals.AuditLog.objects.create",
                side_effect=RuntimeError("forced audit log failure"),
            ):
                response = self.client.post(
                    reverse("rtms_app:treatment_add", args=[self.patient.pk]),
                    post_data,
                )

        self.assertEqual(response.status_code, 302)
        self.assertIn("on_commit()", "\n".join(captured.output))
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "skipped")
        self.assertTrue(
            TreatmentSkip.objects.filter(
                treatment=self.session,
                action_type="postpone",
            ).exists()
        )

    def test_treatment_skip_record_persists_without_filesystem_logging(self):
        with patch("builtins.open") as open_file:
            skip = TreatmentSkip.objects.create(
                treatment=self.session,
                action_type="postpone",
                effective_date=self.session.session_date,
                performed_by=self.user,
            )

        open_file.assert_not_called()
        self.assertTrue(TreatmentSkip.objects.filter(pk=skip.pk).exists())

    def test_undo_post_commit_audit_failures_do_not_retry(self):
        import sqlite3
        from datetime import timedelta
        from django.db import OperationalError, connection
        from rtms_app.models import AuditLog
        from rtms_app.services.rtms_treatment_reservations import _is_sqlite_busy_error

        busy_error = OperationalError("database is locked")
        busy_error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        busy_error.sqlite_errorname = "SQLITE_BUSY"
        self.assertEqual(connection.vendor, "sqlite")
        self.assertTrue(_is_sqlite_busy_error(busy_error))

        original_audit_create = AuditLog.objects.create
        cases = (
            ("normal", None),
            ("runtime", RuntimeError("forced post-commit audit failure")),
            ("sqlite_busy", busy_error),
        )
        for index, (case_name, callback_error) in enumerate(cases):
            with self.subTest(case=case_name):
                session_date = date(2026, 11, 2) + timedelta(days=14 * index)
                session = TreatmentSession.objects.create(
                    patient=self.patient,
                    session_date=session_date,
                    status="planned",
                )
                future = TreatmentSession.objects.create(
                    patient=self.patient,
                    session_date=session_date + timedelta(days=1),
                    status="planned",
                )
                skip_response = self.client.post(
                    reverse("rtms_app:treatment_add", args=[self.patient.pk]),
                    {
                        "treatment_date": session_date.isoformat(),
                        "treatment_time": "09:00",
                        "mt_percent": "120",
                        "frequency_hz": "18.0",
                        "train_seconds": "2.0",
                        "intertrain_seconds": "20.0",
                        "train_count": "55",
                        "total_pulses": "1980",
                        "side_effect_rows_json": "[]",
                        "action": "skip",
                    },
                )
                self.assertEqual(skip_response.status_code, 302)
                skip = TreatmentSkip.objects.get(
                    treatment=session,
                    action_type="postpone",
                )
                audit_count_before = AuditLog.objects.filter(
                    target_model="TreatmentSession",
                    target_pk=str(session.pk),
                ).count()

                if callback_error is None:
                    with patch(
                        "rtms_app.signals.AuditLog.objects.create",
                        wraps=original_audit_create,
                    ) as audit_create:
                        response = self.client.post(
                            reverse("rtms_app:treatment_skip_undo", args=[skip.pk]),
                        )
                    self.assertGreater(audit_create.call_count, 0)
                    self.assertGreater(
                        AuditLog.objects.filter(
                            target_model="TreatmentSession",
                            target_pk=str(session.pk),
                        ).count(),
                        audit_count_before,
                    )
                else:
                    with self.assertLogs(
                        "django.db.backends.base", level="ERROR",
                    ) as captured:
                        with patch(
                            "rtms_app.signals.AuditLog.objects.create",
                            side_effect=callback_error,
                        ) as audit_create:
                            response = self.client.post(
                                reverse("rtms_app:treatment_skip_undo", args=[skip.pk]),
                            )
                    self.assertGreater(audit_create.call_count, 0)
                    self.assertIn("on_commit()", "\n".join(captured.output))
                    self.assertEqual(
                        AuditLog.objects.filter(
                            target_model="TreatmentSession",
                            target_pk=str(session.pk),
                        ).count(),
                        audit_count_before,
                    )

                self.assertEqual(response.status_code, 302)
                session.refresh_from_db()
                future.refresh_from_db()
                skip.refresh_from_db()
                self.assertEqual(session.status, "planned")
                self.assertEqual(session.session_date, session_date)
                self.assertEqual(future.status, "planned")
                self.assertEqual(
                    TreatmentSkip.objects.filter(treatment=session).count(),
                    1,
                )
                self.assertIsNotNone(skip.undone_at)