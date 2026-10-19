from datetime import date, timedelta
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connections
from django.test import TestCase, TransactionTestCase
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


class TreatmentCancelUndoTests(TestCase):
    def setUp(self):
        self.patient = Patient.objects.create(
            card_id="cancel-undo-1",
            name="Cancel Undo Patient",
            birth_date=date(1980, 1, 1),
        )
        self.course = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=1,
        )
        self.user = get_user_model().objects.create_user(
            username="cancel-undo-operator",
            password="pw",
            is_staff=True,
        )
        self.client.force_login(self.user)
        self.start_date = date(2026, 10, 5)

    def make_session(self, offset, status="planned"):
        return TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=self.course,
            course_number=1,
            session_date=self.start_date + timedelta(days=offset),
            status=status,
        )

    def post_cancel(self, treatment_date):
        return self.client.post(
            reverse("rtms_app:treatment_add", args=[self.patient.pk]),
            {
                "course_number": "1",
                "treatment_date": treatment_date.isoformat(),
                "treatment_time": "09:00",
                "mt_percent": "120",
                "intensity_percent": "120",
                "frequency_hz": "18.0",
                "train_seconds": "2.0",
                "intertrain_seconds": "20.0",
                "train_count": "55",
                "total_pulses": "1980",
                "side_effect_rows_json": "[]",
                "action": "cancel",
                "skip_reason": "cancel test",
            },
        )

    def test_cancel_and_undo_preserve_done_and_skipped_history(self):
        selected = self.make_session(0)
        done = self.make_session(1, "done")
        skipped = self.make_session(2, "skipped")
        planned = self.make_session(3)
        old_skip = TreatmentSkip.objects.create(
            treatment=skipped,
            action_type="postpone",
            effective_date=skipped.session_date,
            performed_by=self.user,
        )

        response = self.post_cancel(selected.session_date)

        self.assertEqual(response.status_code, 302)
        selected.refresh_from_db()
        done.refresh_from_db()
        skipped.refresh_from_db()
        planned.refresh_from_db()
        self.assertEqual(selected.status, "skipped")
        self.assertEqual(done.status, "done")
        self.assertEqual(skipped.status, "skipped")
        self.assertEqual(planned.status, "skipped")
        self.assertTrue(TreatmentSkip.objects.filter(pk=old_skip.pk).exists())
        cancel_skip = TreatmentSkip.objects.get(
            treatment=selected,
            action_type="cancel",
        )
        cancel_rows = list(TreatmentSkip.objects.filter(batch_id=cancel_skip.batch_id))
        self.assertEqual(len(cancel_rows), 2)
        self.assertEqual(
            sum(row.snapshot.get("batch_role") == "root" for row in cancel_rows),
            1,
        )
        member = next(row for row in cancel_rows if row.snapshot.get("batch_role") == "member")
        self.assertEqual(member.treatment_id, planned.pk)
        self.assertEqual(member.snapshot["root_skip_id"], cancel_skip.pk)
        self.assertEqual(
            set(cancel_skip.snapshot["target_session_ids"]),
            {selected.pk, planned.pk},
        )

        response = self.client.post(
            reverse("rtms_app:treatment_skip_undo", args=[cancel_skip.pk]),
        )

        self.assertEqual(response.status_code, 302)
        selected.refresh_from_db()
        done.refresh_from_db()
        skipped.refresh_from_db()
        planned.refresh_from_db()
        cancel_skip.refresh_from_db()
        self.assertEqual(selected.status, "planned")
        self.assertEqual(done.status, "done")
        self.assertEqual(skipped.status, "skipped")
        self.assertEqual(planned.status, "planned")
        self.assertIsNotNone(cancel_skip.undone_at)
        self.assertTrue(TreatmentSkip.objects.filter(pk=old_skip.pk).exists())

    def test_history_groups_batch_and_root_undo_restores_all_sessions(self):
        from rtms_app.services.treatment_cancellation import cancel_treatment_sessions

        selected = self.make_session(0)
        future = self.make_session(1)
        root = cancel_treatment_sessions(
            patient_id=self.patient.pk,
            course_number=1,
            treatment_date=selected.session_date,
            user=self.user,
            expected_course_id=self.course.pk,
        )
        member = TreatmentSkip.objects.get(
            batch_id=root.batch_id,
            snapshot__batch_role="member",
        )

        response = self.client.get(
            reverse("rtms_app:treatment_skip_list", args=[self.patient.pk]),
        )

        self.assertEqual(response.status_code, 200)
        batch_rows = [row for row in response.context["skips"] if row.is_cancel_batch]
        self.assertEqual(len(batch_rows), 1)
        self.assertEqual(batch_rows[0].pk, root.pk)
        self.assertEqual(batch_rows[0].batch_count, 2)
        self.assertTrue(batch_rows[0].can_undo)
        self.assertContains(
            response,
            reverse("rtms_app:treatment_skip_undo", args=[root.pk]),
        )
        self.assertNotContains(
            response,
            reverse("rtms_app:treatment_skip_undo", args=[member.pk]),
        )

        response = self.client.post(
            reverse("rtms_app:treatment_skip_undo", args=[root.pk]),
        )

        self.assertEqual(response.status_code, 302)
        selected.refresh_from_db()
        future.refresh_from_db()
        root.refresh_from_db()
        member.refresh_from_db()
        self.assertEqual(selected.status, "planned")
        self.assertEqual(future.status, "planned")
        self.assertIsNotNone(root.undone_at)
        self.assertEqual(member.undone_at, root.undone_at)

    def test_member_history_post_is_rejected_without_database_changes(self):
        from rtms_app.services.treatment_cancellation import cancel_treatment_sessions

        selected = self.make_session(0)
        self.make_session(1)
        root = cancel_treatment_sessions(
            patient_id=self.patient.pk,
            course_number=1,
            treatment_date=selected.session_date,
            user=self.user,
            expected_course_id=self.course.pk,
        )
        member = TreatmentSkip.objects.get(
            batch_id=root.batch_id,
            snapshot__batch_role="member",
        )
        reservation = RtmSTreatmentReservation.objects.create(
            treatment_course=self.course,
            treatment_date=selected.session_date,
            sequence_number=1,
            status="confirmed",
            capacity_type="rTMS_private",
        )
        sessions_before = list(
            TreatmentSession.objects.filter(treatment_course=self.course)
            .order_by("pk")
            .values_list("pk", "status", "session_date")
        )
        reservations_before = list(
            RtmSTreatmentReservation.objects.filter(treatment_course=self.course)
            .order_by("pk")
            .values_list("pk", "treatment_date", "sequence_number", "status", "capacity_type")
        )
        skips_before = list(
            TreatmentSkip.objects.filter(batch_id=root.batch_id)
            .order_by("pk")
            .values_list("pk", "undone_at", "snapshot")
        )

        with patch("rtms_app.views.undo_treatment_cancellation") as undo_batch:
            response = self.client.post(
                reverse("rtms_app:treatment_skip_undo", args=[member.pk]),
            )

        self.assertEqual(response.status_code, 400)
        undo_batch.assert_not_called()
        self.assertEqual(
            list(TreatmentSession.objects.filter(treatment_course=self.course)
                 .order_by("pk").values_list("pk", "status", "session_date")),
            sessions_before,
        )
        self.assertEqual(
            list(RtmSTreatmentReservation.objects.filter(treatment_course=self.course)
                 .order_by("pk").values_list(
                     "pk", "treatment_date", "sequence_number", "status", "capacity_type",
                 )),
            reservations_before,
        )
        self.assertEqual(
            list(TreatmentSkip.objects.filter(batch_id=root.batch_id)
                 .order_by("pk").values_list("pk", "undone_at", "snapshot")),
            skips_before,
        )
        self.assertTrue(RtmSTreatmentReservation.objects.filter(pk=reservation.pk).exists())

    def test_history_batches_are_separate_by_course(self):
        from rtms_app.services.treatment_cancellation import cancel_treatment_sessions

        first_selected = self.make_session(0)
        self.make_session(1)
        second_course = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=2,
        )
        second_selected = TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=second_course,
            course_number=2,
            session_date=self.start_date,
            status="planned",
        )
        TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=second_course,
            course_number=2,
            session_date=self.start_date + timedelta(days=1),
            status="planned",
        )
        first_root = cancel_treatment_sessions(
            patient_id=self.patient.pk,
            course_number=1,
            treatment_date=first_selected.session_date,
            user=self.user,
            expected_course_id=self.course.pk,
        )
        second_root = cancel_treatment_sessions(
            patient_id=self.patient.pk,
            course_number=2,
            treatment_date=second_selected.session_date,
            user=self.user,
            expected_course_id=second_course.pk,
        )

        for course_number, expected_root in ((1, first_root), (2, second_root)):
            with self.subTest(course_number=course_number):
                response = self.client.get(
                    reverse("rtms_app:treatment_skip_list", args=[self.patient.pk]),
                    {"course_number": course_number},
                )
                self.assertEqual(response.status_code, 200)
                rows = response.context["skips"]
                self.assertEqual(len(rows), 1)
                self.assertTrue(rows[0].is_cancel_batch)
                self.assertEqual(rows[0].pk, expected_root.pk)
                self.assertEqual(rows[0].batch_count, 2)

    def test_legacy_and_normal_skip_rows_remain_individual(self):
        sessions = [self.make_session(offset) for offset in range(3)]
        legacy_rows = [
            TreatmentSkip.objects.create(
                treatment=session,
                action_type="cancel",
                effective_date=session.session_date,
                performed_by=self.user,
                snapshot={"version": 1, "kind": "cancel"},
            )
            for session in sessions[:2]
        ]
        normal_row = TreatmentSkip.objects.create(
            treatment=sessions[2],
            action_type="postpone",
            effective_date=sessions[2].session_date,
            performed_by=self.user,
        )

        response = self.client.get(
            reverse("rtms_app:treatment_skip_list", args=[self.patient.pk]),
        )

        rows = response.context["skips"]
        self.assertEqual(len(rows), 3)
        self.assertEqual({row.pk for row in rows}, {item.pk for item in legacy_rows} | {normal_row.pk})
        self.assertTrue(all(not row.is_cancel_batch for row in rows))
        self.assertTrue(all(row.batch_count == 1 and row.can_undo for row in rows))

    def test_malformed_batch_snapshot_hides_undo_without_breaking_history(self):
        from rtms_app.services.treatment_cancellation import cancel_treatment_sessions

        selected = self.make_session(0)
        self.make_session(1)
        root = cancel_treatment_sessions(
            patient_id=self.patient.pk,
            course_number=1,
            treatment_date=selected.session_date,
            user=self.user,
            expected_course_id=self.course.pk,
        )
        root.snapshot["batch_records"][0]["id"] = None
        root.save(update_fields=["snapshot"])

        response = self.client.get(
            reverse("rtms_app:treatment_skip_list", args=[self.patient.pk]),
        )

        self.assertEqual(response.status_code, 200)
        rows = response.context["skips"]
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].is_cancel_batch)
        self.assertFalse(rows[0].can_undo)
        self.assertNotContains(
            response,
            reverse("rtms_app:treatment_skip_undo", args=[root.pk]),
        )

    def test_done_and_skipped_selected_sessions_keep_history_through_undo(self):
        from rtms_app.services.treatment_cancellation import (
            cancel_treatment_sessions,
            undo_treatment_cancellation,
        )

        for offset, status in ((0, "done"), (2, "skipped")):
            with self.subTest(status=status):
                session = self.make_session(offset, status)
                future = self.make_session(offset + 1)
                old_skip = None
                if status == "skipped":
                    old_skip = TreatmentSkip.objects.create(
                        treatment=session,
                        action_type="postpone",
                        effective_date=session.session_date,
                        performed_by=self.user,
                    )

                cancel_skip = cancel_treatment_sessions(
                    patient_id=self.patient.pk,
                    course_number=1,
                    treatment_date=session.session_date,
                    user=self.user,
                    expected_course_id=self.course.pk,
                )

                session.refresh_from_db()
                future.refresh_from_db()
                self.assertEqual(session.status, status)
                self.assertEqual(future.status, "skipped")
                if old_skip is not None:
                    self.assertTrue(TreatmentSkip.objects.filter(pk=old_skip.pk).exists())

                undo_treatment_cancellation(skip_id=cancel_skip.pk, user=self.user)

                session.refresh_from_db()
                future.refresh_from_db()
                self.assertEqual(session.status, status)
                self.assertEqual(future.status, "planned")
                if old_skip is not None:
                    self.assertTrue(TreatmentSkip.objects.filter(pk=old_skip.pk).exists())

    def test_repeated_cancel_and_undo_are_rejected(self):
        selected = self.make_session(0)
        self.assertEqual(self.post_cancel(selected.session_date).status_code, 302)
        self.assertEqual(self.post_cancel(selected.session_date).status_code, 400)
        cancel_skip = next(
            row for row in TreatmentSkip.objects.filter(action_type="cancel")
            if row.snapshot.get("batch_role") == "root"
        )
        undo_url = reverse("rtms_app:treatment_skip_undo", args=[cancel_skip.pk])

        self.assertEqual(self.client.post(undo_url).status_code, 302)
        self.assertEqual(self.client.post(undo_url).status_code, 400)
        self.assertEqual(TreatmentSkip.objects.filter(action_type="cancel").count(), 1)

    def test_undo_rejects_session_changed_after_cancellation(self):
        selected = self.make_session(0)
        self.assertEqual(self.post_cancel(selected.session_date).status_code, 302)
        cancel_skip = next(
            row for row in TreatmentSkip.objects.filter(action_type="cancel")
            if row.snapshot.get("batch_role") == "root"
        )
        selected.treatment_notes = "Changed after cancellation"
        selected.save(update_fields=["treatment_notes"])

        response = self.client.post(
            reverse("rtms_app:treatment_skip_undo", args=[cancel_skip.pk]),
        )

        self.assertEqual(response.status_code, 400)
        selected.refresh_from_db()
        cancel_skip.refresh_from_db()
        self.assertEqual(selected.status, "skipped")
        self.assertIsNone(cancel_skip.undone_at)

    def test_undo_rejects_reservation_changed_after_cancellation(self):
        first = self.make_session(0)
        second = self.make_session(1)
        reservation = RtmSTreatmentReservation.objects.create(
            treatment_course=self.course,
            treatment_date=first.session_date,
            sequence_number=1,
            status="provisional",
            capacity_type="rTMS_private",
        )
        RtmSTreatmentReservation.objects.create(
            treatment_course=self.course,
            treatment_date=second.session_date,
            sequence_number=2,
            status="provisional",
            capacity_type="rTMS_private",
        )
        self.assertEqual(self.post_cancel(first.session_date).status_code, 302)
        cancel_skip = next(
            row for row in TreatmentSkip.objects.filter(action_type="cancel")
            if row.snapshot.get("batch_role") == "root"
        )
        reservation.status = "confirmed"
        reservation.save(update_fields=["status", "updated_at"])

        response = self.client.post(
            reverse("rtms_app:treatment_skip_undo", args=[cancel_skip.pk]),
        )

        self.assertEqual(response.status_code, 400)
        first.refresh_from_db()
        cancel_skip.refresh_from_db()
        self.assertEqual(first.status, "skipped")
        self.assertIsNone(cancel_skip.undone_at)

    def test_legacy_cancel_snapshot_is_rejected_without_writes(self):
        selected = self.make_session(0)
        cancel_skip = TreatmentSkip.objects.create(
            treatment=selected,
            action_type="cancel",
            effective_date=selected.session_date,
            performed_by=self.user,
            snapshot={
                "affected_sessions": [{
                    "id": selected.pk,
                    "session_date": selected.session_date.isoformat(),
                    "date": selected.date.isoformat(),
                }],
            },
        )
        self.assertIsNone(cancel_skip.batch_id)

        response = self.client.post(
            reverse("rtms_app:treatment_skip_undo", args=[cancel_skip.pk]),
        )

        self.assertEqual(response.status_code, 400)
        selected.refresh_from_db()
        cancel_skip.refresh_from_db()
        self.assertEqual(selected.status, "planned")
        self.assertIsNone(cancel_skip.undone_at)
        self.assertIsNone(cancel_skip.batch_id)

    def test_malformed_versioned_snapshot_is_rejected_without_writes(self):
        selected = self.make_session(0)
        cancel_skip = TreatmentSkip.objects.create(
            treatment=selected,
            action_type="cancel",
            effective_date=selected.session_date,
            performed_by=self.user,
            snapshot={
                "version": 1,
                "kind": "cancel",
                "patient_id": self.patient.pk,
                "treatment_course_id": self.course.pk,
                "course_number": 1,
                "selected_session_id": selected.pk,
                "effective_date": "not-a-date",
            },
        )

        response = self.client.post(
            reverse("rtms_app:treatment_skip_undo", args=[cancel_skip.pk]),
        )

        self.assertEqual(response.status_code, 400)
        selected.refresh_from_db()
        cancel_skip.refresh_from_db()
        self.assertEqual(selected.status, "planned")
        self.assertIsNone(cancel_skip.undone_at)

    def test_undo_from_member_restores_the_entire_batch(self):
        selected = self.make_session(0)
        future = self.make_session(1)

        from rtms_app.services.treatment_cancellation import (
            cancel_treatment_sessions,
            undo_treatment_cancellation,
        )
        root = cancel_treatment_sessions(
            patient_id=self.patient.pk,
            course_number=1,
            treatment_date=selected.session_date,
            user=self.user,
            expected_course_id=self.course.pk,
        )
        member = next(
            row for row in TreatmentSkip.objects.filter(batch_id=root.batch_id)
            if row.snapshot.get("batch_role") == "member"
        )

        undo_treatment_cancellation(skip_id=member.pk, user=self.user)

        selected.refresh_from_db()
        future.refresh_from_db()
        root.refresh_from_db()
        member.refresh_from_db()
        self.assertEqual(selected.status, "planned")
        self.assertEqual(future.status, "planned")
        self.assertIsNotNone(root.undone_at)
        self.assertEqual(member.undone_at, root.undone_at)

    def test_cancel_without_planned_targets_keeps_one_root_record(self):
        selected = self.make_session(0, "done")

        from rtms_app.services.treatment_cancellation import cancel_treatment_sessions
        root = cancel_treatment_sessions(
            patient_id=self.patient.pk,
            course_number=1,
            treatment_date=selected.session_date,
            user=self.user,
            expected_course_id=self.course.pk,
        )

        self.assertEqual(root.snapshot["batch_role"], "root")
        self.assertEqual(root.snapshot["target_session_ids"], [])
        self.assertEqual(TreatmentSkip.objects.filter(batch_id=root.batch_id).count(), 1)
        selected.refresh_from_db()
        self.assertEqual(selected.status, "done")

    def test_member_history_failure_rolls_back_the_entire_cancellation(self):
        selected = self.make_session(0)
        future = self.make_session(1)
        create_record = TreatmentSkip.objects.create

        def create_root_then_fail_member(**kwargs):
            if kwargs["snapshot"].get("batch_role") == "member":
                raise RuntimeError("member history failed")
            return create_record(**kwargs)

        from rtms_app.services.treatment_cancellation import cancel_treatment_sessions
        with patch.object(
            TreatmentSkip.objects,
            "create",
            side_effect=create_root_then_fail_member,
        ):
            with self.assertRaisesRegex(RuntimeError, "member history failed"):
                cancel_treatment_sessions(
                    patient_id=self.patient.pk,
                    course_number=1,
                    treatment_date=selected.session_date,
                    user=self.user,
                    expected_course_id=self.course.pk,
                )

        selected.refresh_from_db()
        future.refresh_from_db()
        self.assertEqual(selected.status, "planned")
        self.assertEqual(future.status, "planned")
        self.assertFalse(TreatmentSkip.objects.filter(action_type="cancel").exists())

    def test_course_two_undo_does_not_restore_patient_discharge_mirror(self):
        from rtms_app.services.treatment_cancellation import (
            cancel_treatment_sessions,
            undo_treatment_cancellation,
        )

        second_course = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=2,
            discharge_date=date(2026, 10, 30),
        )
        selected = TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=second_course,
            course_number=2,
            session_date=self.start_date,
            status="planned",
        )
        self.patient.discharge_date = date(2026, 11, 1)
        self.patient.save(update_fields=["discharge_date"])
        cancel_skip = cancel_treatment_sessions(
            patient_id=self.patient.pk,
            course_number=2,
            treatment_date=selected.session_date,
            user=self.user,
            expected_course_id=second_course.pk,
        )
        self.patient.discharge_date = date(2026, 11, 2)
        self.patient.save(update_fields=["discharge_date"])

        undo_treatment_cancellation(skip_id=cancel_skip.pk, user=self.user)

        selected.refresh_from_db()
        second_course.refresh_from_db()
        self.patient.refresh_from_db()
        self.assertEqual(selected.status, "planned")
        self.assertEqual(second_course.discharge_date, date(2026, 10, 30))
        self.assertEqual(self.patient.discharge_date, date(2026, 11, 2))

    def test_reservation_sync_failure_rolls_back_cancellation(self):
        from rtms_app.services.rtms_schedule import generate_treatment_dates
        from rtms_app.services.treatment_cancellation import cancel_treatment_sessions
        from rtms_app.services.rtms_treatment_reservations import (
            sync_rtms_treatment_reservation_from_sessions,
        )

        dates = generate_treatment_dates(self.start_date, total=30)
        for treatment_date in dates:
            self.make_session((treatment_date - self.start_date).days)
        selected = TreatmentSession.objects.get(
            treatment_course=self.course,
            session_date=dates[0],
        )
        original_reservation = RtmSTreatmentReservation.objects.create(
            treatment_course=self.course,
            treatment_date=dates[0],
            sequence_number=1,
            status="provisional",
            capacity_type="rTMS_private",
        )
        self.course.discharge_date = date(2026, 12, 31)
        self.course.save(update_fields=["discharge_date"])

        def sync_then_fail(**kwargs):
            sync_rtms_treatment_reservation_from_sessions(**kwargs)
            raise RuntimeError("sync failed")

        with patch(
            "rtms_app.services.treatment_cancellation.sync_rtms_treatment_reservation_from_sessions",
            side_effect=sync_then_fail,
        ):
            with self.assertRaisesRegex(RuntimeError, "sync failed"):
                cancel_treatment_sessions(
                    patient_id=self.patient.pk,
                    course_number=1,
                    treatment_date=selected.session_date,
                    user=self.user,
                    expected_course_id=self.course.pk,
                )

        selected.refresh_from_db()
        self.course.refresh_from_db()
        self.assertEqual(selected.status, "planned")
        self.assertEqual(self.course.discharge_date, date(2026, 12, 31))
        self.assertFalse(TreatmentSkip.objects.filter(action_type="cancel").exists())
        self.assertEqual(
            TreatmentSession.objects.filter(treatment_course=self.course, status="planned").count(),
            30,
        )
        self.assertEqual(
            RtmSTreatmentReservation.objects.filter(treatment_course=self.course).count(),
            1,
        )
        original_reservation.refresh_from_db()
        self.assertEqual(original_reservation.status, "provisional")
        self.assertEqual(original_reservation.treatment_date, dates[0])

    def test_cancel_does_not_change_course_two_sessions_or_reservations(self):
        second_course = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=2,
        )
        second_session = TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=second_course,
            course_number=2,
            session_date=self.start_date,
            status="planned",
        )
        reservation = RtmSTreatmentReservation.objects.create(
            treatment_course=second_course,
            treatment_date=self.start_date,
            sequence_number=1,
            status="confirmed",
            capacity_type="rTMS_private",
        )
        selected = self.make_session(0)

        self.assertEqual(self.post_cancel(selected.session_date).status_code, 302)

        second_session.refresh_from_db()
        reservation.refresh_from_db()
        self.assertEqual(second_session.status, "planned")
        self.assertEqual(reservation.status, "confirmed")
        self.assertEqual(reservation.treatment_course_id, second_course.pk)

    def test_cancel_and_undo_restore_existing_thirty_reservations(self):
        from rtms_app.services.rtms_schedule import generate_treatment_dates
        from rtms_app.services.treatment_cancellation import (
            cancel_treatment_sessions,
            undo_treatment_cancellation,
        )

        dates = generate_treatment_dates(self.start_date, total=30)
        sessions = [
            self.make_session((treatment_date - self.start_date).days)
            for treatment_date in dates
        ]
        original_statuses = ["confirmed"] * 30
        original_types = ["rTMS_private" if index % 2 == 0 else "general_ward" for index in range(30)]
        RtmSTreatmentReservation.objects.bulk_create([
            RtmSTreatmentReservation(
                treatment_course=self.course,
                treatment_date=treatment_date,
                sequence_number=index,
                status=original_statuses[index - 1],
                capacity_type=original_types[index - 1],
            )
            for index, treatment_date in enumerate(dates, start=1)
        ])

        cancel_skip = cancel_treatment_sessions(
            patient_id=self.patient.pk,
            course_number=1,
            treatment_date=dates[0],
            user=self.user,
            expected_course_id=self.course.pk,
        )
        self.assertEqual(
            set(RtmSTreatmentReservation.objects.filter(
                treatment_course=self.course,
            ).values_list("status", flat=True)),
            {"cancelled"},
        )

        undo_treatment_cancellation(skip_id=cancel_skip.pk, user=self.user)

        reservations = list(
            RtmSTreatmentReservation.objects.filter(treatment_course=self.course)
            .order_by("sequence_number")
        )
        self.assertEqual(len(reservations), 30)
        self.assertEqual(
            [item.status for item in reservations],
            original_statuses,
        )
        self.assertEqual(
            [item.capacity_type for item in reservations],
            original_types,
        )
        self.assertEqual(
            [item.treatment_date for item in reservations],
            dates,
        )
        self.assertTrue(all(session.status == "planned" for session in sessions))

    def test_cancel_created_reservations_are_retained_cancelled_by_undo(self):
        from rtms_app.services.rtms_schedule import generate_treatment_dates
        from rtms_app.services.treatment_cancellation import (
            cancel_treatment_sessions,
            undo_treatment_cancellation,
        )

        dates = generate_treatment_dates(self.start_date, total=30)
        for treatment_date in dates:
            self.make_session((treatment_date - self.start_date).days)

        cancel_skip = cancel_treatment_sessions(
            patient_id=self.patient.pk,
            course_number=1,
            treatment_date=dates[0],
            user=self.user,
            expected_course_id=self.course.pk,
        )
        self.assertEqual(
            RtmSTreatmentReservation.objects.filter(treatment_course=self.course).count(),
            30,
        )

        undo_treatment_cancellation(skip_id=cancel_skip.pk, user=self.user)

        reservations = list(
            RtmSTreatmentReservation.objects.filter(treatment_course=self.course)
        )
        self.assertEqual(len(reservations), 30)
        self.assertTrue(all(item.status == "cancelled" for item in reservations))


class TreatmentCancellationConcurrencyTests(TransactionTestCase):
    reset_sequences = True

    def test_parallel_cancellations_for_one_patient_are_serialized(self):
        patient = Patient.objects.create(
            card_id="cancel-concurrency-1",
            name="Cancel Concurrency Patient",
            birth_date=date(1980, 1, 1),
        )
        course = TreatmentCourse.objects.create(patient=patient, course_number=1)
        user = get_user_model().objects.create_user(
            username="cancel-concurrency-operator",
            password="pw",
        )
        first_date = date(2026, 10, 5)
        second_date = first_date + timedelta(days=1)
        TreatmentSession.objects.create(
            patient=patient,
            treatment_course=course,
            course_number=1,
            session_date=first_date,
            status="planned",
        )
        TreatmentSession.objects.create(
            patient=patient,
            treatment_course=course,
            course_number=1,
            session_date=second_date,
            status="planned",
        )
        barrier = Barrier(2)

        def cancel_in_thread(treatment_date):
            connections.close_all()
            try:
                barrier.wait(timeout=5)
                from rtms_app.services.treatment_cancellation import (
                    TreatmentCancellationError,
                    cancel_treatment_sessions,
                )
                try:
                    cancel_treatment_sessions(
                        patient_id=patient.pk,
                        course_number=1,
                        treatment_date=treatment_date,
                        user=user,
                        expected_course_id=course.pk,
                    )
                    return "cancelled"
                except TreatmentCancellationError:
                    return "rejected"
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(cancel_in_thread, (first_date, second_date)))

        self.assertEqual(outcomes.count("cancelled") + outcomes.count("rejected"), 2)
        self.assertGreaterEqual(outcomes.count("cancelled"), 1)
        self.assertTrue(
            TreatmentSession.objects.filter(
                treatment_course=course,
                status="skipped",
            ).count() == 2
        )
        cancel_skips = TreatmentSkip.objects.filter(
            treatment__treatment_course=course,
            action_type="cancel",
        )
        self.assertIn(cancel_skips.count(), (1, 2))
        self.assertEqual(
            cancel_skips.values("treatment_id").distinct().count(),
            cancel_skips.count(),
        )


class TreatmentCancellationPostCommitAuditTests(TransactionTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="cancel-post-commit-operator",
            password="pw",
        )
        self.client.force_login(self.user)
        RtmSTreatmentCapacity.objects.bulk_create([
            RtmSTreatmentCapacity(
                capacity_type="rTMS_private",
                capacity=10,
                valid_from=date(2026, 1, 1),
            ),
            RtmSTreatmentCapacity(
                capacity_type="general_ward",
                capacity=10,
                valid_from=date(2026, 1, 1),
            ),
        ])

    def _make_course_fixture(self, index):
        patient = Patient.objects.create(
            card_id=f"R6C{index:02d}",
            name="Post Commit Cancel Patient",
            birth_date=date(1980, 1, 1),
        )
        course = TreatmentCourse.objects.create(
            patient=patient,
            course_number=1,
        )
        selected_date = date(2026, 10, 5) + timedelta(days=14 * index)
        selected = TreatmentSession.objects.create(
            patient=patient,
            treatment_course=course,
            course_number=1,
            session_date=selected_date,
            status="planned",
        )
        future = TreatmentSession.objects.create(
            patient=patient,
            treatment_course=course,
            course_number=1,
            session_date=selected_date + timedelta(days=1),
            status="planned",
        )
        mapping = MappingSchedule.objects.create(
            patient=patient,
            treatment_course=course,
            course_number=1,
            week_number=1,
            planned_date=selected_date,
        )
        reservations = [
            RtmSTreatmentReservation.objects.create(
                treatment_course=course,
                treatment_date=session.session_date,
                sequence_number=sequence_number,
                status="confirmed",
                capacity_type="rTMS_private",
            )
            for sequence_number, session in enumerate((selected, future), start=1)
        ]
        return patient, course, selected, future, mapping, reservations

    def _callback_cases(self):
        import sqlite3
        from django.db import OperationalError, connection
        from rtms_app.services.rtms_treatment_reservations import _is_sqlite_busy_error

        busy_error = OperationalError("database is locked")
        busy_error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        busy_error.sqlite_errorname = "SQLITE_BUSY"
        self.assertEqual(connection.vendor, "sqlite")
        self.assertTrue(_is_sqlite_busy_error(busy_error))
        return (
            ("normal", None),
            ("runtime", RuntimeError("forced post-commit audit failure")),
            ("sqlite_busy", busy_error),
        )

    def _post_cancel(self, patient, treatment_date):
        return self.client.post(
            reverse("rtms_app:treatment_add", args=[patient.pk]),
            {
                "course_number": "1",
                "treatment_date": treatment_date.isoformat(),
                "treatment_time": "09:00",
                "mt_percent": "120",
                "intensity_percent": "120",
                "frequency_hz": "18.0",
                "train_seconds": "2.0",
                "intertrain_seconds": "20.0",
                "train_count": "55",
                "total_pulses": "1980",
                "side_effect_rows_json": "[]",
                "action": "cancel",
            },
        )

    def _post_commit_request(self, callback_error, request):
        from rtms_app.models import AuditLog

        original_audit_create = AuditLog.objects.create
        if callback_error is None:
            with patch(
                "rtms_app.signals.AuditLog.objects.create",
                wraps=original_audit_create,
            ) as audit_create:
                response = request()
            self.assertGreater(audit_create.call_count, 0)
            return response

        with self.assertLogs("django.db.backends.base", level="ERROR") as captured:
            with patch(
                "rtms_app.signals.AuditLog.objects.create",
                side_effect=callback_error,
            ) as audit_create:
                response = request()
        self.assertGreater(audit_create.call_count, 0)
        self.assertIn("on_commit()", "\n".join(captured.output))
        return response

    def test_cancel_post_commit_audit_failures_do_not_retry(self):
        from rtms_app.models import AuditLog

        for index, (case_name, callback_error) in enumerate(self._callback_cases()):
            with self.subTest(case=case_name):
                patient, course, selected, future, mapping, reservations = (
                    self._make_course_fixture(index)
                )
                response = self._post_commit_request(
                    callback_error,
                    lambda: self._post_cancel(patient, selected.session_date),
                )

                self.assertEqual(response.status_code, 302)
                selected.refresh_from_db()
                future.refresh_from_db()
                self.assertEqual(selected.status, "skipped")
                self.assertEqual(future.status, "skipped")
                cancel_rows = list(
                    TreatmentSkip.objects.filter(
                        treatment__treatment_course=course,
                        action_type="cancel",
                    ).order_by("pk")
                )
                self.assertEqual(len(cancel_rows), 2)
                self.assertEqual(
                    sum(row.snapshot.get("batch_role") == "root" for row in cancel_rows),
                    1,
                )
                self.assertTrue(all(row.undone_at is None for row in cancel_rows))
                self.assertEqual(
                    list(
                        RtmSTreatmentReservation.objects.filter(treatment_course=course)
                        .order_by("sequence_number")
                        .values_list("pk", "treatment_date", "status")
                    ),
                    [(row.pk, row.treatment_date, "cancelled") for row in reservations],
                )
                mapping.refresh_from_db()
                self.assertEqual(mapping.planned_date, selected.session_date)
                if callback_error is None:
                    self.assertTrue(AuditLog.objects.filter(
                        target_model="TreatmentSession",
                        target_pk=str(selected.pk),
                    ).exists())

    def test_cancel_undo_post_commit_audit_failures_do_not_retry(self):
        from rtms_app.models import AuditLog

        for index, (case_name, callback_error) in enumerate(self._callback_cases()):
            with self.subTest(case=case_name):
                patient, course, selected, future, mapping, reservations = (
                    self._make_course_fixture(index)
                )
                cancel_response = self._post_cancel(patient, selected.session_date)
                self.assertEqual(cancel_response.status_code, 302)
                root = TreatmentSkip.objects.get(
                    treatment=selected,
                    action_type="cancel",
                    snapshot__batch_role="root",
                )
                audit_count_before = AuditLog.objects.filter(
                    target_model="TreatmentSession",
                    target_pk=str(selected.pk),
                ).count()
                response = self._post_commit_request(
                    callback_error,
                    lambda: self.client.post(
                        reverse("rtms_app:treatment_skip_undo", args=[root.pk]),
                    ),
                )

                self.assertEqual(response.status_code, 302)
                selected.refresh_from_db()
                future.refresh_from_db()
                self.assertEqual(selected.status, "planned")
                self.assertEqual(future.status, "planned")
                cancel_rows = list(
                    TreatmentSkip.objects.filter(batch_id=root.batch_id).order_by("pk")
                )
                self.assertEqual(len(cancel_rows), 2)
                self.assertEqual(
                    {row.undone_at for row in cancel_rows}.__len__(),
                    1,
                )
                self.assertIsNotNone(cancel_rows[0].undone_at)
                self.assertEqual(
                    list(
                        RtmSTreatmentReservation.objects.filter(treatment_course=course)
                        .order_by("sequence_number")
                        .values_list("pk", "treatment_date", "status")
                    ),
                    [(row.pk, row.treatment_date, "confirmed") for row in reservations],
                )
                mapping.refresh_from_db()
                self.assertEqual(mapping.planned_date, selected.session_date)
                audit_count_after = AuditLog.objects.filter(
                    target_model="TreatmentSession",
                    target_pk=str(selected.pk),
                ).count()
                if callback_error is None:
                    self.assertGreater(audit_count_after, audit_count_before)
                else:
                    self.assertEqual(audit_count_after, audit_count_before)