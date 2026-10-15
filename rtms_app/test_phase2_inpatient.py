from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from threading import Barrier, Event, Thread
from types import SimpleNamespace
import os
import re
import sqlite3
import tempfile
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import IntegrityError, connections, connection, transaction
from django.db.models import F
from django.test import TestCase, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone as django_timezone

from rtms_app.models import (
    AuditLog,
    InpatientPlan,
    MappingSchedule,
    MappingSession,
    Patient,
    Assessment,
    AssessmentRecord,
    ResourceAssignment,
    ResourcePool,
    RtmSAdmissionCapacity,
    RtmSTreatmentCapacity,
    RtmSTreatmentReservation,
    RtmSWaitlistEntry,
    TreatmentCourse,
    TreatmentSession,
)
from rtms_app.services.inpatient_planning import (
    ActiveResourceAssignmentError,
    ActualAdmissionError,
    ActualDischargeError,
    confirm_inpatient_schedule,
    InvalidWaitlistEntryError,
    ScheduleConfirmationBusyError,
    record_actual_admission,
    record_actual_discharge,
    rollback_provisional_schedule,
    update_calendar_course_adjustment,
)
from rtms_app.services.resource_capacity import (
    CapacityConflict,
    capacity_snapshot,
    earliest_admission_forecast,
)
from rtms_app.services.rtms_treatment_reservations import (
    TreatmentReservationBusyError,
    create_or_replace_rtms_treatment_reservation,
    treatment_capacity_snapshot,
)
from rtms_app.services.rtms_schedule import (
    generate_course_treatment_dates,
    generate_treatment_dates,
    get_japanese_holidays,
    is_closed,
)
from rtms_app.services.rtms_inpatient_calendar import (
    add_new_course_start_options,
    _weekly_rtms_course_counts,
    build_inpatient_calendar,
    build_inpatient_calendar_range,
)
from rtms_app.services.waitlist import (
    WaitlistAlreadyRegisteredError,
    register_waitlist_entry,
)
from rtms_app.views import generate_calendar_weeks, get_patient_admission_status


class _SQLiteBusyCause(Exception):
    sqlite_errorcode = sqlite3.SQLITE_BUSY
    sqlite_errorname = "SQLITE_BUSY"


class Phase2InpatientTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="phase2-user", password="pw")
        self.client.force_login(self.user)
        self.patient = Patient.objects.create(
            card_id="82001", name="Phase Two", birth_date=date(1980, 1, 1),
        )
        self.course_one = TreatmentCourse.objects.create(patient=self.patient, course_number=1)
        self.course_two = TreatmentCourse.objects.create(patient=self.patient, course_number=2)
        self.pool = ResourcePool.objects.create(
            code="PHASE2_PRIVATE", name="rTMS個室", resource_type="rTMS_private",
            physical_capacity=5, operational_target=3,
        )
        self.capacity = RtmSAdmissionCapacity.objects.create(
            capacity=3, valid_from=date(2026, 10, 1), valid_to=date(2026, 10, 31),
        )

    def make_provisional_schedule(
        self, course=None, *, private_room_planned=None,
        is_all_case_survey=False, planned_treatment_sessions=30,
    ):
        course = course or self.course_one
        RtmSTreatmentCapacity.objects.filter(is_active=True).update(capacity=100)
        entry = RtmSWaitlistEntry.objects.create(
            treatment_course=course,
            status="waiting",
            registered_by=self.user,
        )
        update_calendar_course_adjustment(
            treatment_course=course,
            admission_date=date(2026, 10, 2),
            treatment_start_date=date(2026, 10, 5),
            is_all_case_survey=is_all_case_survey,
            private_room_planned=private_room_planned,
            planned_treatment_sessions=planned_treatment_sessions,
            waitlist_entry=entry,
            user=self.user,
        )
        entry.refresh_from_db()
        course.refresh_from_db()
        return entry

    def post_provisional_rollback(self, course, entry):
        return self.client.post(
            reverse("rtms_app:rollback_provisional_waitlist"),
            {"course_id": str(course.pk), "waitlist_entry_id": str(entry.pk)},
        )

    def test_clean_provisional_can_return_to_waiting_and_reappear_in_waitlist(self):
        self.course_one.is_all_case_survey = True
        self.course_one.private_room_planned = True
        self.course_one.planned_treatment_sessions = 18
        self.course_one.save(update_fields=[
            "is_all_case_survey", "private_room_planned",
            "planned_treatment_sessions", "updated_at",
        ])
        entry = self.make_provisional_schedule(
            private_room_planned=False,
            is_all_case_survey=False,
            planned_treatment_sessions=30,
        )
        reservation_ids = list(
            RtmSTreatmentReservation.objects.filter(
                treatment_course=self.course_one,
            ).values_list("pk", flat=True)
        )

        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"week_start": "2026-10-05"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-rollback-open")
        self.assertContains(response, "rollbackConfirmDialog")
        self.assertContains(response, "治療実績は変更されません")

        rollback_response = self.post_provisional_rollback(self.course_one, entry)

        self.assertEqual(rollback_response.status_code, 302)
        entry.refresh_from_db()
        self.course_one.refresh_from_db()
        plan = InpatientPlan.objects.get(treatment_course=self.course_one)
        self.assertEqual(entry.status, "waiting")
        self.assertIsNone(entry.inpatient_plan_id)
        self.assertEqual(plan.status, "draft")
        self.assertIsNone(plan.planned_admission_date)
        self.assertIsNone(self.course_one.admission_date)
        self.assertIsNone(self.course_one.first_treatment_date)
        self.assertIsNone(self.course_one.mapping_date)
        self.assertTrue(self.course_one.is_all_case_survey)
        self.assertTrue(self.course_one.private_room_planned)
        self.assertEqual(self.course_one.planned_treatment_sessions, 18)
        reservations = RtmSTreatmentReservation.objects.filter(
            treatment_course=self.course_one,
        )
        self.assertEqual(list(reservations.values_list("pk", flat=True)), reservation_ids)
        self.assertFalse(reservations.exclude(status="cancelled").exists())
        self.assertTrue(AuditLog.objects.filter(
            target_model="RtmSWaitlistEntry",
            target_pk=str(entry.pk),
            meta__event="provisional_schedule_rollback",
            meta__course_id=self.course_one.pk,
            meta__inpatient_plan_id=plan.pk,
        ).exists())

        displayed = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"week_start": "2026-10-05"},
        )
        self.assertEqual(list(displayed.context["waitlist_entries"]), [entry])
        self.assertEqual(
            len(re.findall(rb"<button\b[^>]*\bdata-rollback-open(?:\s|=|>)", displayed.content)),
            0,
        )
        self.assertEqual(displayed.context["treatment_calendar_rows"], [])

    def test_rollback_requires_provisional_state_and_matching_course(self):
        entry = self.make_provisional_schedule()
        mismatch = self.post_provisional_rollback(self.course_two, entry)
        self.assertEqual(mismatch.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.status, "provisional")

        entry.status = "waiting"
        entry.save(update_fields=["status"])
        non_provisional = self.post_provisional_rollback(self.course_one, entry)
        self.assertEqual(non_provisional.status_code, 302)
        self.course_one.refresh_from_db()
        self.assertEqual(self.course_one.first_treatment_date, date(2026, 10, 5))

    def test_rollback_rejects_sessions_assignments_mapping_and_confirmed_reservations(self):
        for blocker in ("session", "assignment", "mapping", "confirmed_reservation"):
            with self.subTest(blocker=blocker):
                course = TreatmentCourse.objects.create(
                    patient=self.patient,
                    course_number=10 + ("session", "assignment", "mapping", "confirmed_reservation").index(blocker),
                )
                entry = self.make_provisional_schedule(course)
                plan = InpatientPlan.objects.get(treatment_course=course)
                if blocker == "session":
                    TreatmentSession.objects.create(
                        patient=self.patient, treatment_course=course,
                        course_number=course.course_number,
                        session_date=date(2026, 10, 5), status="planned",
                    )
                elif blocker == "assignment":
                    ResourceAssignment.objects.create(
                        treatment_course=course, resource_pool=self.pool,
                        planned_start_date=date(2026, 10, 2),
                        planned_end_date=date(2026, 10, 10), status="planned",
                    )
                elif blocker == "mapping":
                    MappingSession.objects.create(
                        patient=self.patient, treatment_course=course,
                        course_number=course.course_number, resting_mt=50,
                    )
                else:
                    RtmSTreatmentReservation.objects.filter(
                        treatment_course=course, sequence_number=1,
                    ).update(status="confirmed")

                response = self.post_provisional_rollback(course, entry)

                self.assertEqual(response.status_code, 302)
                entry.refresh_from_db()
                course.refresh_from_db()
                plan.refresh_from_db()
                self.assertEqual(entry.status, "provisional")
                self.assertEqual(plan.status, "provisional")
                self.assertEqual(course.first_treatment_date, date(2026, 10, 5))

    def test_rollback_rejects_course_dates_that_were_not_null_before_booking(self):
        self.course_one.admission_date = date(2026, 9, 30)
        self.course_one.mapping_date = date(2026, 10, 1)
        self.course_one.save(update_fields=["admission_date", "mapping_date", "updated_at"])
        entry = self.make_provisional_schedule()

        response = self.post_provisional_rollback(self.course_one, entry)

        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.course_one.refresh_from_db()
        self.assertEqual(entry.status, "provisional")
        self.assertEqual(self.course_one.admission_date, date(2026, 10, 2))
        self.assertEqual(self.course_one.mapping_date, date(2026, 10, 1))

    def test_rollback_rejects_provisional_rows_without_a_baseline_audit(self):
        self.course_one.admission_date = date(2026, 10, 2)
        self.course_one.first_treatment_date = date(2026, 10, 5)
        self.course_one.save(update_fields=["admission_date", "first_treatment_date", "updated_at"])
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="provisional",
            planned_admission_date=date(2026, 10, 2),
        )
        entry = RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="provisional",
            inpatient_plan=plan,
        )

        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"week_start": "2026-10-05"},
        )
        rollback_response = self.post_provisional_rollback(self.course_one, entry)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            len(re.findall(rb"<button\b[^>]*\bdata-rollback-open(?:\s|=|>)", response.content)),
            0,
        )
        self.assertEqual(rollback_response.status_code, 302)
        entry.refresh_from_db()
        plan.refresh_from_db()
        self.course_one.refresh_from_db()
        self.assertEqual(entry.status, "provisional")
        self.assertEqual(plan.status, "provisional")
        self.assertEqual(self.course_one.first_treatment_date, date(2026, 10, 5))

    def test_rollback_rejects_malformed_or_mismatched_baseline_metadata(self):
        cases = (
            "root_not_object",
            "missing_course_before",
            "missing_course_date_key",
            "missing_entry_snapshot_key",
            "course_id_wrong_type",
            "course_id_mismatch",
            "entry_id_mismatch",
            "course_before_wrong_type",
            "plan_snapshot_id_mismatch",
            "plan_baseline_snapshot_id_mismatch",
        )
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                course = TreatmentCourse.objects.create(
                    patient=self.patient, course_number=10 + index,
                )
                if case == "plan_baseline_snapshot_id_mismatch":
                    InpatientPlan.objects.create(treatment_course=course, status="draft")
                entry = self.make_provisional_schedule(course)
                baseline_log = AuditLog.objects.get(
                    target_model="RtmSWaitlistEntry",
                    target_pk=str(entry.pk),
                    meta__event="provisional_schedule_baseline",
                )
                meta = baseline_log.meta
                if case == "root_not_object":
                    baseline_log.meta = []
                elif case == "missing_course_before":
                    meta.pop("course_before")
                elif case == "missing_course_date_key":
                    meta["course_before"].pop("admission_date")
                elif case == "missing_entry_snapshot_key":
                    meta["provisional_values"]["entry"].pop("scheduled_at")
                elif case == "course_id_wrong_type":
                    meta["course_id"] = str(course.pk)
                elif case == "course_id_mismatch":
                    meta["course_id"] = self.course_one.pk
                elif case == "entry_id_mismatch":
                    meta["waitlist_entry_id"] = entry.pk + 1000
                elif case == "course_before_wrong_type":
                    meta["course_before"] = []
                elif case == "plan_baseline_snapshot_id_mismatch":
                    meta["plan_before"]["id"] += 1000
                else:
                    meta["provisional_values"]["plan"]["id"] += 1000
                if case != "root_not_object":
                    baseline_log.meta = meta
                baseline_log.save(update_fields=["meta"])

                response = self.post_provisional_rollback(course, entry)

                self.assertEqual(response.status_code, 302)
                entry.refresh_from_db()
                self.assertEqual(entry.status, "provisional")

    def test_rollback_rejects_corrupt_baseline_json_read(self):
        entry = self.make_provisional_schedule()
        with patch.object(AuditLog.objects, "filter", side_effect=ValueError("invalid JSON")):
            response = self.post_provisional_rollback(self.course_one, entry)

        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.status, "provisional")

    def test_rollback_rejects_baseline_plan_linked_to_another_course(self):
        foreign_plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="draft",
        )
        entry = RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_two,
            status="waiting",
            inpatient_plan=foreign_plan,
        )
        update_calendar_course_adjustment(
            treatment_course=self.course_two,
            admission_date=date(2026, 10, 2),
            treatment_start_date=date(2026, 10, 5),
            is_all_case_survey=False,
            waitlist_entry=entry,
            user=self.user,
        )
        entry.refresh_from_db()
        course_two_plan = InpatientPlan.objects.get(treatment_course=self.course_two)
        self.assertEqual(entry.inpatient_plan_id, course_two_plan.pk)

        response = self.post_provisional_rollback(self.course_two, entry)

        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.status, "provisional")
        self.assertEqual(entry.inpatient_plan_id, course_two_plan.pk)
        self.assertNotEqual(entry.inpatient_plan_id, foreign_plan.pk)

    def test_rollback_rejects_changes_to_course_entry_or_plan_snapshot(self):
        for index, field in enumerate(("course", "entry", "plan")):
            with self.subTest(field=field):
                course = TreatmentCourse.objects.create(
                    patient=self.patient, course_number=20 + index,
                )
                entry = self.make_provisional_schedule(course)
                plan = InpatientPlan.objects.get(treatment_course=course)
                if field == "course":
                    course.is_all_case_survey = True
                    course.save(update_fields=["is_all_case_survey", "updated_at"])
                elif field == "entry":
                    entry.scheduled_at = django_timezone.now()
                    entry.save(update_fields=["scheduled_at", "updated_at"])
                else:
                    plan.uncertainty = "unknown"
                    plan.save(update_fields=["uncertainty", "updated_at"])

                response = self.post_provisional_rollback(course, entry)

                self.assertEqual(response.status_code, 302)
                entry.refresh_from_db()
                self.assertEqual(entry.status, "provisional")

    def test_rollback_preserves_existing_mapping_schedule(self):
        mapping_schedule = MappingSchedule.objects.create(
            patient=self.patient,
            treatment_course=self.course_one,
            course_number=self.course_one.course_number,
            week_number=1,
            planned_date=date(2026, 10, 6),
        )
        entry = self.make_provisional_schedule(self.course_two)

        response = self.post_provisional_rollback(self.course_two, entry)

        self.assertEqual(response.status_code, 302)
        mapping_schedule.refresh_from_db()
        self.assertEqual(mapping_schedule.planned_date, date(2026, 10, 6))

    def test_rollback_rejects_course_and_legacy_mapping_schedules(self):
        for index, legacy in enumerate((False, True)):
            with self.subTest(legacy=legacy):
                course = TreatmentCourse.objects.create(
                    patient=self.patient, course_number=30 + index,
                )
                mapping_schedule = MappingSchedule.objects.create(
                    patient=self.patient,
                    treatment_course=None if legacy else course,
                    course_number=course.course_number,
                    week_number=1,
                    planned_date=date(2026, 10, 6),
                )
                entry = self.make_provisional_schedule(course)

                response = self.post_provisional_rollback(course, entry)

                self.assertEqual(response.status_code, 302)
                entry.refresh_from_db()
                mapping_schedule.refresh_from_db()
                self.assertEqual(entry.status, "provisional")
                self.assertEqual(mapping_schedule.planned_date, date(2026, 10, 6))

    def test_rollback_rejects_actual_admission(self):
        entry = self.make_provisional_schedule()
        plan = InpatientPlan.objects.get(treatment_course=self.course_one)
        plan.actual_admission_at = django_timezone.now()
        plan.save(update_fields=["actual_admission_at"])

        response = self.post_provisional_rollback(self.course_one, entry)

        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        plan.refresh_from_db()
        self.assertEqual(entry.status, "provisional")
        self.assertIsNotNone(plan.actual_admission_at)

    def test_course_one_rollback_does_not_change_course_two(self):
        waiting_entry = RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_two, status="waiting",
        )
        provisional_entry = self.make_provisional_schedule(self.course_one)
        self.course_two.admission_date = date(2026, 9, 29)
        self.course_two.first_treatment_date = date(2026, 9, 30)
        self.course_two.save(update_fields=["admission_date", "first_treatment_date", "updated_at"])

        response = self.post_provisional_rollback(self.course_one, provisional_entry)

        self.assertEqual(response.status_code, 302)
        waiting_entry.refresh_from_db()
        self.course_two.refresh_from_db()
        self.assertEqual(waiting_entry.status, "waiting")
        self.assertEqual(self.course_two.admission_date, date(2026, 9, 29))
        self.assertEqual(self.course_two.first_treatment_date, date(2026, 9, 30))

    def test_rollback_preserves_course_two_patient_mirrors_and_course_one(self):
        self.patient.first_treatment_date = date(2026, 8, 3)
        self.patient.mapping_date = date(2026, 8, 4)
        self.patient.save(update_fields=["first_treatment_date", "mapping_date"])
        waiting_entry = RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one, status="waiting",
        )
        provisional_entry = self.make_provisional_schedule(self.course_two)

        response = self.post_provisional_rollback(self.course_two, provisional_entry)

        self.assertEqual(response.status_code, 302)
        self.patient.refresh_from_db()
        self.course_one.refresh_from_db()
        waiting_entry.refresh_from_db()
        self.assertEqual(self.patient.first_treatment_date, date(2026, 8, 3))
        self.assertEqual(self.patient.mapping_date, date(2026, 8, 4))
        self.assertEqual(waiting_entry.status, "waiting")
        self.assertIsNone(self.course_one.first_treatment_date)

    def test_rollback_reuses_cancelled_reservations_on_rebooking(self):
        entry = self.make_provisional_schedule()
        other_entry = self.make_provisional_schedule(self.course_two)
        reservation_ids = list(
            RtmSTreatmentReservation.objects.filter(
                treatment_course=self.course_one,
            ).values_list("pk", flat=True)
        )
        other_reservations_before = list(
            RtmSTreatmentReservation.objects.filter(
                treatment_course=self.course_two,
            ).order_by("sequence_number").values_list(
                "pk", "sequence_number", "treatment_date", "status", "capacity_type",
            )
        )
        self.post_provisional_rollback(self.course_one, entry)
        entry.refresh_from_db()
        self.course_one.refresh_from_db()

        result = update_calendar_course_adjustment(
            treatment_course=self.course_one,
            admission_date=date(2026, 10, 2),
            treatment_start_date=date(2026, 10, 13),
            is_all_case_survey=False,
            waitlist_entry=entry,
            user=self.user,
        )

        entry.refresh_from_db()
        self.assertEqual(entry.status, "provisional")
        reservations = list(RtmSTreatmentReservation.objects.filter(
            treatment_course=self.course_one,
        ).order_by("sequence_number"))
        self.assertEqual(len(reservations), 30)
        self.assertEqual([item.pk for item in reservations], reservation_ids)
        self.assertEqual([item.treatment_course_id for item in reservations], [self.course_one.pk] * 30)
        self.assertEqual([item.sequence_number for item in reservations], list(range(1, 31)))
        self.assertTrue(all(item.status == "provisional" for item in reservations))
        self.assertEqual(
            [item.treatment_date for item in reservations],
            generate_treatment_dates(
                date(2026, 10, 13), total=30,
                holidays=get_japanese_holidays(date(2026, 10, 13), date(2027, 10, 13)),
            ),
        )
        self.assertEqual(len({item.treatment_date for item in reservations}), 30)
        self.assertEqual(
            [item.capacity_type for item in reservations],
            [item.capacity_type for item in result["reservation"]["reservations"]],
        )
        self.assertEqual(
            list(RtmSTreatmentReservation.objects.filter(
                treatment_course=self.course_two,
            ).order_by("sequence_number").values_list(
                "pk", "sequence_number", "treatment_date", "status", "capacity_type",
            )),
            other_reservations_before,
        )
        self.assertEqual(other_entry.status, "provisional")

    def test_rollback_audit_failure_rolls_back_all_course_state(self):
        entry = self.make_provisional_schedule()
        plan = InpatientPlan.objects.get(treatment_course=self.course_one)

        with patch.object(AuditLog.objects, "create", side_effect=RuntimeError("audit unavailable")):
            with self.assertRaises(RuntimeError):
                rollback_provisional_schedule(
                    treatment_course=self.course_one,
                    waitlist_entry=entry,
                    user=self.user,
                )

        entry.refresh_from_db()
        plan.refresh_from_db()
        self.course_one.refresh_from_db()
        self.assertEqual(entry.status, "provisional")
        self.assertEqual(plan.status, "provisional")
        self.assertEqual(self.course_one.first_treatment_date, date(2026, 10, 5))

    def test_independent_capacity_values_and_operational_warning(self):
        self.assertEqual(self.pool.physical_capacity, 5)
        self.assertEqual(self.pool.operational_target, 3)
        self.assertEqual(self.capacity.capacity, 3)
        for index in range(3):
            course = TreatmentCourse.objects.create(
                patient=Patient.objects.create(
                    card_id=f"8200{index + 2}", name=f"Room {index}", birth_date=date(1980, 1, 1),
                ),
                course_number=1,
            )
            ResourceAssignment.objects.create(
                treatment_course=course, resource_pool=self.pool,
                planned_start_date=date(2026, 10, 5), planned_end_date=date(2026, 10, 15),
            )
        snapshot = capacity_snapshot(self.pool, date(2026, 10, 5), date(2026, 10, 15))
        self.assertEqual(snapshot["planned_count"], 3)
        self.assertTrue(snapshot["physical_capacity_available"])
        self.assertTrue(snapshot["operational_target_warning"])

    def test_schedule_confirmation_creates_plan_and_assignment_without_sessions(self):
        result = confirm_inpatient_schedule(
            treatment_course=self.course_one,
            resource_pool=self.pool,
            admission_date=date(2026, 10, 5),
            treatment_start_date=date(2026, 10, 6),
            room_start_date=date(2026, 10, 5),
            room_end_date=date(2026, 10, 15),
            planned_discharge_date=date(2026, 10, 20),
            user=self.user,
        )
        self.assertEqual(result["plan"].status, "scheduled")
        self.assertEqual(result["assignment"].planned_start_date, date(2026, 10, 5))
        self.assertEqual(ResourceAssignment.objects.count(), 1)
        self.assertEqual(TreatmentSession.objects.count(), 0)
        self.assertEqual(result["plan"].status, "scheduled")
        self.assertIsNone(result["plan"].actual_admission_at)
        self.course_one.refresh_from_db()
        self.assertEqual(self.course_one.first_treatment_date, date(2026, 10, 6))

    def test_schedule_confirmation_transitions_provisional_plan_to_scheduled(self):
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="provisional",
            planned_admission_date=date(2026, 10, 1),
        )

        confirm_inpatient_schedule(
            treatment_course=self.course_one,
            resource_pool=self.pool,
            admission_date=date(2026, 10, 5),
            treatment_start_date=date(2026, 10, 6),
            room_start_date=date(2026, 10, 5),
            room_end_date=date(2026, 10, 15),
            planned_discharge_date=date(2026, 10, 20),
            user=self.user,
        )

        plan.refresh_from_db()
        self.assertEqual(plan.status, "scheduled")
        self.assertIsNone(plan.actual_admission_at)
        self.assertEqual(plan.planned_admission_date, date(2026, 10, 5))

    def test_waitlist_confirmation_post_schedules_only_the_explicit_course(self):
        for capacity_type, _label in RtmSTreatmentCapacity.CAPACITY_TYPE_CHOICES:
            RtmSTreatmentCapacity.objects.filter(
                capacity_type=capacity_type,
                valid_from=date(2026, 1, 1),
            ).update(capacity=100)
        self.course_one.admission_date = date(2026, 10, 5)
        self.course_one.first_treatment_date = date(2026, 10, 6)
        self.course_one.save(update_fields=["admission_date", "first_treatment_date", "updated_at"])
        self.course_two.admission_date = date(2026, 10, 7)
        self.course_two.first_treatment_date = date(2026, 10, 8)
        self.course_two.save(update_fields=["admission_date", "first_treatment_date", "updated_at"])
        entry_one = RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="waiting",
        )
        entry_two = RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_two,
            status="provisional",
        )

        for course, entry, admission_date, treatment_date, room_end in (
            (self.course_one, entry_one, "2026-10-05", "2026-10-06", "2026-10-15"),
            (self.course_two, entry_two, "2026-10-07", "2026-10-08", "2026-10-17"),
        ):
            confirmation_url = (
                f"{reverse('rtms_app:inpatient_schedule', args=[self.patient.pk])}"
                f"?course_number={course.course_number}&waitlist_confirmation=1"
            )
            get_response = self.client.get(confirmation_url)
            self.assertEqual(get_response.status_code, 200)
            self.assertContains(get_response, "日程を正式に確定")
            self.assertContains(get_response, "30回分のrTMS予約")
            confirmed_at_before = django_timezone.now()
            post_response = self.client.post(
                confirmation_url,
                {
                    "course_number": str(course.course_number),
                    "waitlist_confirmation": "1",
                    "admission_date": admission_date,
                    "treatment_start_date": treatment_date,
                    "room_start_date": admission_date,
                    "room_end_date": room_end,
                    "certainty": "confirmed",
                    "resource_pool": str(self.pool.pk),
                },
            )
            confirmed_at_after = django_timezone.now()
            self.assertEqual(post_response.status_code, 302)
            entry.refresh_from_db()
            self.assertEqual(entry.status, "scheduled")
            self.assertIsNotNone(entry.scheduled_at)
            self.assertLessEqual(confirmed_at_before, entry.scheduled_at)
            self.assertLessEqual(entry.scheduled_at, confirmed_at_after)
            self.assertEqual(entry.inpatient_plan_id, course.inpatient_plan.pk)
            self.assertEqual(
                RtmSTreatmentReservation.objects.filter(
                    treatment_course=course,
                    status="confirmed",
                ).count(),
                30,
            )
            self.assertTrue(
                ResourceAssignment.objects.filter(
                    treatment_course=course,
                    resource_pool=self.pool,
                    status="planned",
                ).exists()
            )

    def test_waitlist_confirmation_rejects_course_mismatch_and_scheduled_entry(self):
        entry_one = RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="waiting",
        )
        entry_two = RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_two,
            status="scheduled",
        )
        confirmation_url = (
            f"{reverse('rtms_app:inpatient_schedule', args=[self.patient.pk])}"
            "?course_number=1&waitlist_confirmation=1"
        )

        mismatch_response = self.client.post(
            confirmation_url,
            {
                "course_number": "2",
                "waitlist_confirmation": "1",
                "admission_date": "2026-10-05",
                "treatment_start_date": "2026-10-06",
                "room_start_date": "2026-10-05",
                "resource_pool": str(self.pool.pk),
            },
        )
        scheduled_response = self.client.get(
            f"{reverse('rtms_app:inpatient_schedule', args=[self.patient.pk])}"
            "?course_number=2&waitlist_confirmation=1"
        )

        self.assertEqual(mismatch_response.status_code, 400)
        self.assertEqual(scheduled_response.status_code, 400)
        entry_one.refresh_from_db()
        entry_two.refresh_from_db()
        self.assertEqual(entry_one.status, "waiting")
        self.assertEqual(entry_two.status, "scheduled")
        self.assertFalse(InpatientPlan.objects.exists())
        self.assertFalse(ResourceAssignment.objects.exists())
        self.assertFalse(RtmSTreatmentReservation.objects.exists())

    def test_schedule_confirmation_after_admission_preserves_actual_and_updates_plan(self):
        self.patient.status = "waiting"
        self.patient.admission_date = date(2026, 8, 1)
        self.patient.save(update_fields=["status", "admission_date"])
        self.course_one.admission_date = date(2026, 8, 1)
        self.course_one.save(update_fields=["admission_date", "updated_at"])
        course_one_plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="provisional",
            planned_admission_date=date(2026, 9, 20),
        )
        course_two_plan = InpatientPlan.objects.create(
            treatment_course=self.course_two,
            status="scheduled",
            planned_admission_date=date(2026, 9, 21),
        )
        record_actual_admission(treatment_course=self.course_two, user=self.user)
        course_two_plan.refresh_from_db()
        recorded_at = course_two_plan.actual_admission_at

        confirm_inpatient_schedule(
            treatment_course=self.course_two,
            resource_pool=self.pool,
            admission_date=date(2026, 10, 7),
            treatment_start_date=date(2026, 10, 8),
            room_start_date=date(2026, 10, 7),
            room_end_date=date(2026, 10, 17),
            planned_discharge_date=date(2026, 10, 22),
            estimated_discharge_date=date(2026, 10, 21),
            user=self.user,
        )

        course_two_plan.refresh_from_db()
        course_one_plan.refresh_from_db()
        self.patient.refresh_from_db()
        self.course_one.refresh_from_db()
        self.assertEqual(course_two_plan.status, "admitted")
        self.assertEqual(course_two_plan.actual_admission_at, recorded_at)
        self.assertEqual(course_two_plan.planned_admission_date, date(2026, 10, 7))
        self.assertEqual(course_two_plan.planned_discharge_date, date(2026, 10, 22))
        self.assertEqual(course_two_plan.estimated_discharge_date, date(2026, 10, 21))
        self.assertEqual(course_one_plan.status, "provisional")
        self.assertEqual(course_one_plan.planned_admission_date, date(2026, 9, 20))
        self.assertEqual(self.patient.status, "waiting")
        self.assertEqual(self.patient.admission_date, date(2026, 8, 1))
        self.assertEqual(self.course_one.admission_date, date(2026, 8, 1))

    def test_record_actual_admission_sets_timestamp_and_admitted_status(self):
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="scheduled",
            planned_admission_date=date(2026, 9, 20),
        )
        admission_date = self.patient.admission_date
        course_admission_date = self.course_one.admission_date
        patient_status = self.patient.status

        result = record_actual_admission(treatment_course=self.course_one, user=self.user)

        plan.refresh_from_db()
        self.assertFalse(result["already_recorded"])
        self.assertIsNotNone(plan.actual_admission_at)
        self.assertEqual(plan.status, "admitted")
        self.assertEqual(plan.updated_by, self.user)
        self.patient.refresh_from_db()
        self.course_one.refresh_from_db()
        self.assertEqual(self.patient.admission_date, admission_date)
        self.assertEqual(self.patient.status, patient_status)
        self.assertEqual(self.course_one.admission_date, course_admission_date)

    def test_record_actual_admission_is_idempotent_when_status_is_admitted(self):
        recorded_at = datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc)
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="admitted",
            planned_admission_date=date(2026, 9, 20),
            actual_admission_at=recorded_at,
        )

        result = record_actual_admission(treatment_course=self.course_one, user=self.user)

        plan.refresh_from_db()
        self.assertTrue(result["already_recorded"])
        self.assertEqual(plan.actual_admission_at, recorded_at)
        self.assertEqual(plan.status, "admitted")

    def test_record_actual_admission_rejects_existing_timestamp_with_wrong_status(self):
        recorded_at = datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc)
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="scheduled",
            actual_admission_at=recorded_at,
        )

        with self.assertRaisesMessage(
            ActualAdmissionError,
            "実入院日時と入院計画の状態が一致しません。管理者に確認してください。",
        ):
            record_actual_admission(treatment_course=self.course_one, user=self.user)

        plan.refresh_from_db()
        self.assertEqual(plan.status, "scheduled")
        self.assertEqual(plan.actual_admission_at, recorded_at)

    def test_record_actual_admission_requires_existing_plan(self):
        with self.assertRaisesMessage(
            ActualAdmissionError,
            "入院予定が登録されていないため、実入院を記録できません。",
        ):
            record_actual_admission(treatment_course=self.course_one, user=self.user)

        self.assertFalse(InpatientPlan.objects.filter(treatment_course=self.course_one).exists())
        self.assertEqual(ResourceAssignment.objects.count(), 0)

    def test_course_two_actual_admission_does_not_change_course_one_or_patient(self):
        self.patient.admission_date = date(2026, 8, 1)
        self.patient.status = "waiting"
        self.patient.save(update_fields=["admission_date", "status"])
        self.course_one.admission_date = date(2026, 8, 1)
        self.course_one.save(update_fields=["admission_date", "updated_at"])
        course_one_plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="provisional",
            planned_admission_date=date(2026, 9, 20),
        )
        course_two_plan = InpatientPlan.objects.create(
            treatment_course=self.course_two,
            status="scheduled",
            planned_admission_date=date(2026, 9, 21),
        )

        record_actual_admission(treatment_course=self.course_two, user=self.user)

        course_one_plan.refresh_from_db()
        course_two_plan.refresh_from_db()
        self.patient.refresh_from_db()
        self.course_one.refresh_from_db()
        self.assertIsNone(course_one_plan.actual_admission_at)
        self.assertEqual(course_one_plan.status, "provisional")
        self.assertIsNotNone(course_two_plan.actual_admission_at)
        self.assertEqual(course_two_plan.status, "admitted")
        self.assertEqual(self.patient.admission_date, date(2026, 8, 1))
        self.assertEqual(self.patient.status, "waiting")
        self.assertEqual(self.course_one.admission_date, date(2026, 8, 1))
        self.assertFalse(ResourceAssignment.objects.exists())

    def test_admission_procedure_does_not_record_actual_admission(self):
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="scheduled",
            planned_admission_date=date(2026, 9, 20),
        )

        response = self.client.post(
            reverse("rtms_app:admission_procedure", args=[self.patient.pk]),
            {"course_number": self.course_one.course_number, "admission_type": "voluntary"},
        )

        self.assertEqual(response.status_code, 302)
        plan.refresh_from_db()
        self.assertIsNone(plan.actual_admission_at)
        self.assertEqual(plan.status, "scheduled")
        self.course_one.refresh_from_db()
        self.assertTrue(self.course_one.is_admission_procedure_done)

    def test_past_planned_admission_date_does_not_set_actual_admission(self):
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="scheduled",
            planned_admission_date=date(2026, 9, 20),
        )

        plan.refresh_from_db()
        self.assertIsNone(plan.actual_admission_at)
        self.assertEqual(plan.status, "scheduled")

    def test_actual_admission_endpoint_is_separate_and_course_scoped(self):
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_two,
            status="scheduled",
            planned_admission_date=date(2026, 9, 20),
        )

        response = self.client.post(
            reverse("rtms_app:record_actual_admission", args=[self.patient.pk]),
            {"course_number": self.course_two.course_number},
        )

        self.assertRedirects(
            response,
            f"{reverse('rtms_app:inpatient_schedule', args=[self.patient.pk])}?patient_id={self.patient.pk}"
            f"&course_number={self.course_two.course_number}",
        )
        plan.refresh_from_db()
        self.assertEqual(plan.status, "admitted")
        self.assertIsNotNone(plan.actual_admission_at)
        self.assertEqual(ResourceAssignment.objects.count(), 0)

    def test_inpatient_schedule_shows_actual_admission_separately(self):
        response = self.client.get(
            reverse("rtms_app:inpatient_schedule", args=[self.patient.pk]),
            {"course_number": self.course_one.course_number},
        )

        self.assertContains(response, "入院予定が登録されていないため")
        self.assertNotContains(response, "実入院を記録</button>")

        InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="scheduled",
            planned_admission_date=date(2026, 9, 20),
        )
        scheduled_response = self.client.get(
            reverse("rtms_app:inpatient_schedule", args=[self.patient.pk]),
            {"course_number": self.course_one.course_number},
        )
        self.assertContains(scheduled_response, "実入院を記録")

    def test_record_actual_discharge_sets_timestamp_near_now_and_preserves_other_fields(self):
        recorded_admission_at = datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc)
        existing_discharge_at = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="admitted",
            actual_admission_at=recorded_admission_at,
            planned_discharge_date=date(2026, 10, 20),
            estimated_discharge_date=date(2026, 10, 19),
        )
        self.patient.discharge_date = date(2026, 10, 21)
        self.patient.status = "inpatient"
        self.patient.save(update_fields=["discharge_date", "status"])
        self.course_one.discharge_date = date(2026, 10, 22)
        self.course_one.discharged_at = existing_discharge_at
        self.course_one.save(update_fields=["discharge_date", "discharged_at", "updated_at"])
        assignment = ResourceAssignment.objects.create(
            treatment_course=self.course_one,
            resource_pool=self.pool,
            planned_start_date=date(2026, 10, 5),
            planned_end_date=date(2026, 10, 20),
            actual_end_at=existing_discharge_at,
            status="active",
            certainty="provisional",
        )
        before = django_timezone.now()

        result = record_actual_discharge(treatment_course=self.course_one, user=self.user)

        after = django_timezone.now()
        plan.refresh_from_db()
        self.patient.refresh_from_db()
        self.course_one.refresh_from_db()
        assignment.refresh_from_db()
        self.assertFalse(result["already_recorded"])
        self.assertEqual(plan.status, "discharged")
        self.assertGreaterEqual(plan.actual_discharge_at, before)
        self.assertLessEqual(plan.actual_discharge_at, after)
        self.assertEqual(plan.updated_by, self.user)
        self.assertEqual(plan.actual_admission_at, recorded_admission_at)
        self.assertEqual(plan.planned_discharge_date, date(2026, 10, 20))
        self.assertEqual(plan.estimated_discharge_date, date(2026, 10, 19))
        self.assertEqual(self.patient.status, "inpatient")
        self.assertEqual(self.patient.discharge_date, date(2026, 10, 21))
        self.assertEqual(self.course_one.discharge_date, date(2026, 10, 22))
        self.assertEqual(self.course_one.discharged_at, existing_discharge_at)
        self.assertEqual(assignment.actual_end_at, existing_discharge_at)
        self.assertEqual(assignment.status, "active")
        self.assertEqual(assignment.certainty, "provisional")

    def test_record_actual_discharge_is_idempotent(self):
        recorded_at = datetime(2026, 9, 30, 10, 15, tzinfo=timezone.utc)
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="admitted",
            actual_admission_at=datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc),
        )

        first = record_actual_discharge(treatment_course=self.course_one, user=self.user)
        plan.refresh_from_db()
        first_recorded_at = plan.actual_discharge_at
        second = record_actual_discharge(treatment_course=self.course_one, user=self.user)

        plan.refresh_from_db()
        self.assertFalse(first["already_recorded"])
        self.assertTrue(second["already_recorded"])
        self.assertIsNotNone(first_recorded_at)
        self.assertEqual(plan.status, "discharged")
        self.assertEqual(plan.actual_discharge_at, first_recorded_at)
        self.assertNotEqual(plan.actual_discharge_at, recorded_at)

    def test_record_actual_discharge_rejects_existing_timestamp_with_wrong_status(self):
        recorded_at = datetime(2026, 9, 30, 10, 15, tzinfo=timezone.utc)
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="admitted",
            actual_admission_at=datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc),
            actual_discharge_at=recorded_at,
        )

        with self.assertRaises(ActualDischargeError):
            record_actual_discharge(treatment_course=self.course_one, user=self.user)

        plan.refresh_from_db()
        self.assertEqual(plan.status, "admitted")
        self.assertEqual(plan.actual_discharge_at, recorded_at)

    def test_record_actual_discharge_rejects_discharged_status_without_timestamp(self):
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="discharged",
            actual_admission_at=datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc),
        )

        with self.assertRaises(ActualDischargeError):
            record_actual_discharge(treatment_course=self.course_one, user=self.user)

        plan.refresh_from_db()
        self.assertEqual(plan.status, "discharged")
        self.assertIsNone(plan.actual_discharge_at)

    def test_record_actual_discharge_requires_existing_plan(self):
        with self.assertRaisesMessage(
            ActualDischargeError,
            "入院計画が登録されていないため、実退院を記録できません。",
        ):
            record_actual_discharge(treatment_course=self.course_one, user=self.user)

        self.assertFalse(InpatientPlan.objects.filter(treatment_course=self.course_one).exists())

    def test_record_actual_discharge_requires_consistent_admitted_plan(self):
        for status, admission_at in (
            ("scheduled", None),
            ("provisional", None),
            ("admitted", None),
            ("scheduled", datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc)),
        ):
            with self.subTest(status=status, admission_at=admission_at):
                InpatientPlan.objects.filter(treatment_course=self.course_one).delete()
                InpatientPlan.objects.create(
                    treatment_course=self.course_one,
                    status=status,
                    actual_admission_at=admission_at,
                )
                with self.assertRaises(ActualDischargeError):
                    record_actual_discharge(treatment_course=self.course_one, user=self.user)

                plan = InpatientPlan.objects.get(treatment_course=self.course_one)
                self.assertIsNone(plan.actual_discharge_at)
                self.assertEqual(plan.status, status)

    def test_course_two_actual_discharge_does_not_change_course_one_or_patient(self):
        self.patient.status = "inpatient"
        self.patient.discharge_date = date(2026, 10, 25)
        self.patient.save(update_fields=["status", "discharge_date"])
        self.course_one.discharge_date = date(2026, 10, 24)
        self.course_one.save(update_fields=["discharge_date", "updated_at"])
        course_one_plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="admitted",
            actual_admission_at=datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc),
        )
        course_two_plan = InpatientPlan.objects.create(
            treatment_course=self.course_two,
            status="admitted",
            actual_admission_at=datetime(2026, 9, 26, 9, 30, tzinfo=timezone.utc),
        )

        record_actual_discharge(treatment_course=self.course_two, user=self.user)

        course_one_plan.refresh_from_db()
        course_two_plan.refresh_from_db()
        self.patient.refresh_from_db()
        self.course_one.refresh_from_db()
        self.assertEqual(course_one_plan.status, "admitted")
        self.assertIsNone(course_one_plan.actual_discharge_at)
        self.assertEqual(course_two_plan.status, "discharged")
        self.assertIsNotNone(course_two_plan.actual_discharge_at)
        self.assertEqual(self.patient.status, "inpatient")
        self.assertEqual(self.patient.discharge_date, date(2026, 10, 25))
        self.assertEqual(self.course_one.discharge_date, date(2026, 10, 24))

    def test_course_one_actual_discharge_does_not_change_course_two(self):
        course_one_plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="admitted",
            actual_admission_at=datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc),
        )
        course_two_plan = InpatientPlan.objects.create(
            treatment_course=self.course_two,
            status="admitted",
            actual_admission_at=datetime(2026, 9, 26, 9, 30, tzinfo=timezone.utc),
        )

        record_actual_discharge(treatment_course=self.course_one, user=self.user)

        course_one_plan.refresh_from_db()
        course_two_plan.refresh_from_db()
        self.assertEqual(course_one_plan.status, "discharged")
        self.assertIsNotNone(course_one_plan.actual_discharge_at)
        self.assertEqual(course_two_plan.status, "admitted")
        self.assertIsNone(course_two_plan.actual_discharge_at)

    def test_actual_discharge_endpoint_is_course_scoped_and_duplicate_safe(self):
        course_one_plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="admitted",
            actual_admission_at=datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc),
        )
        course_two_plan = InpatientPlan.objects.create(
            treatment_course=self.course_two,
            status="admitted",
            actual_admission_at=datetime(2026, 9, 26, 9, 30, tzinfo=timezone.utc),
        )
        url = reverse("rtms_app:record_actual_discharge", args=[self.patient.pk])

        first_response = self.client.post(url, {"course_number": self.course_two.course_number})
        self.assertEqual(first_response.status_code, 302)
        course_two_plan.refresh_from_db()
        first_recorded_at = course_two_plan.actual_discharge_at
        second_response = self.client.post(
            url,
            {"course_number": self.course_two.course_number},
            follow=True,
        )

        self.assertEqual(second_response.status_code, 200)
        self.assertContains(second_response, "実退院実績はすでに登録されています")
        course_one_plan.refresh_from_db()
        course_two_plan.refresh_from_db()
        self.assertEqual(course_one_plan.status, "admitted")
        self.assertIsNone(course_one_plan.actual_discharge_at)
        self.assertEqual(course_two_plan.status, "discharged")
        self.assertEqual(course_two_plan.actual_discharge_at, first_recorded_at)
        self.assertRedirects(
            first_response,
            f"{reverse('rtms_app:inpatient_schedule', args=[self.patient.pk])}"
            f"?patient_id={self.patient.pk}&course_number={self.course_two.course_number}",
        )

    def test_inpatient_schedule_separates_actual_discharge_from_planned_date(self):
        admitted_plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="admitted",
            actual_admission_at=datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc),
            planned_discharge_date=date(2026, 10, 20),
        )
        response = self.client.get(
            reverse("rtms_app:inpatient_schedule", args=[self.patient.pk]),
            {"course_number": self.course_one.course_number},
        )
        self.assertContains(response, "退院予定日")
        self.assertContains(response, "実退院を記録")
        self.assertNotContains(response, "実退院記録済み")

        admitted_plan.actual_discharge_at = datetime(2026, 10, 1, 11, tzinfo=timezone.utc)
        admitted_plan.status = "discharged"
        admitted_plan.save(update_fields=["actual_discharge_at", "status", "updated_at"])
        discharged_response = self.client.get(
            reverse("rtms_app:inpatient_schedule", args=[self.patient.pk]),
            {"course_number": self.course_one.course_number},
        )
        self.assertContains(discharged_response, "実退院記録済み")
        self.assertNotContains(discharged_response, "実退院を記録</button>")

    def test_admission_procedure_does_not_record_actual_discharge(self):
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="scheduled",
            planned_admission_date=date(2026, 9, 20),
        )

        response = self.client.post(
            reverse("rtms_app:admission_procedure", args=[self.patient.pk]),
            {"course_number": self.course_one.course_number, "admission_type": "voluntary"},
        )

        self.assertEqual(response.status_code, 302)
        plan.refresh_from_db()
        self.assertIsNone(plan.actual_discharge_at)
        self.assertEqual(plan.status, "scheduled")

    def test_past_planned_discharge_date_does_not_record_actual_discharge(self):
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="admitted",
            actual_admission_at=datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc),
            planned_discharge_date=date(2026, 9, 1),
        )

        plan.refresh_from_db()
        self.assertIsNone(plan.actual_discharge_at)
        self.assertEqual(plan.status, "admitted")

    def test_schedule_confirmation_after_discharge_preserves_actual_and_updates_plan(self):
        recorded_discharge_at = datetime(2026, 9, 30, 10, 15, tzinfo=timezone.utc)
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="discharged",
            actual_admission_at=datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc),
            actual_discharge_at=recorded_discharge_at,
            planned_discharge_date=date(2026, 10, 10),
            estimated_discharge_date=date(2026, 10, 9),
        )

        confirm_inpatient_schedule(
            treatment_course=self.course_one,
            resource_pool=self.pool,
            admission_date=date(2026, 10, 5),
            treatment_start_date=date(2026, 10, 6),
            room_start_date=date(2026, 10, 5),
            room_end_date=date(2026, 10, 15),
            planned_discharge_date=date(2026, 10, 12),
            estimated_discharge_date=date(2026, 10, 11),
            user=self.user,
        )

        plan.refresh_from_db()
        self.assertEqual(plan.status, "discharged")
        self.assertEqual(plan.actual_discharge_at, recorded_discharge_at)
        self.assertEqual(plan.planned_discharge_date, date(2026, 10, 12))
        self.assertEqual(plan.estimated_discharge_date, date(2026, 10, 11))

    def test_schedule_confirmation_with_actual_discharge_repairs_status_and_preserves_actual(self):
        recorded_discharge_at = datetime(2026, 9, 30, 10, 15, tzinfo=timezone.utc)
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="scheduled",
            actual_admission_at=datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc),
            actual_discharge_at=recorded_discharge_at,
        )

        confirm_inpatient_schedule(
            treatment_course=self.course_one,
            resource_pool=self.pool,
            admission_date=date(2026, 10, 5),
            treatment_start_date=date(2026, 10, 6),
            room_start_date=date(2026, 10, 5),
            room_end_date=date(2026, 10, 15),
            planned_discharge_date=date(2026, 10, 12),
            estimated_discharge_date=date(2026, 10, 11),
            user=self.user,
        )

        plan.refresh_from_db()
        self.assertEqual(plan.status, "discharged")
        self.assertEqual(plan.actual_discharge_at, recorded_discharge_at)
        self.assertEqual(plan.planned_discharge_date, date(2026, 10, 12))
        self.assertEqual(plan.estimated_discharge_date, date(2026, 10, 11))

    def test_schedule_confirmation_preserves_cancelled_status(self):
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="cancelled",
        )

        confirm_inpatient_schedule(
            treatment_course=self.course_one,
            resource_pool=self.pool,
            admission_date=date(2026, 10, 5),
            treatment_start_date=date(2026, 10, 6),
            room_start_date=date(2026, 10, 5),
            room_end_date=date(2026, 10, 15),
            planned_discharge_date=date(2026, 10, 12),
            user=self.user,
        )

        plan.refresh_from_db()
        self.assertEqual(plan.status, "cancelled")
        self.assertEqual(plan.planned_discharge_date, date(2026, 10, 12))
        self.assertFalse(
            RtmSTreatmentReservation.objects.filter(
                treatment_course=self.course_one,
            ).exists()
        )

    def test_schedule_confirmation_for_course_one_does_not_change_course_two(self):
        course_one_plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="scheduled",
            planned_discharge_date=date(2026, 10, 20),
        )
        course_two_plan = InpatientPlan.objects.create(
            treatment_course=self.course_two,
            status="admitted",
            actual_admission_at=datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc),
            planned_discharge_date=date(2026, 10, 22),
        )

        confirm_inpatient_schedule(
            treatment_course=self.course_one,
            resource_pool=self.pool,
            admission_date=date(2026, 10, 5),
            treatment_start_date=date(2026, 10, 6),
            room_start_date=date(2026, 10, 5),
            room_end_date=date(2026, 10, 15),
            planned_discharge_date=date(2026, 10, 12),
            user=self.user,
        )

        course_one_plan.refresh_from_db()
        course_two_plan.refresh_from_db()
        self.assertEqual(course_one_plan.status, "scheduled")
        self.assertEqual(course_one_plan.planned_discharge_date, date(2026, 10, 12))
        self.assertEqual(course_two_plan.status, "admitted")
        self.assertEqual(course_two_plan.planned_discharge_date, date(2026, 10, 22))
        self.assertEqual(
            course_two_plan.actual_admission_at,
            datetime(2026, 9, 25, 9, 30, tzinfo=timezone.utc),
        )



    def test_discharge_preparation_syncs_existing_plan_as_planned_discharge(self):
        actual_discharge_at = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="scheduled",
            planned_admission_date=date(2026, 10, 1),
            estimated_discharge_date=date(2026, 10, 20),
            actual_discharge_at=actual_discharge_at,
        )
        response = self.client.post(
            reverse("rtms_app:patient_home", args=[self.patient.pk]),
            {
                "course_number": self.course_one.course_number,
                "discharge_date": "2026-10-15",
                "summary_text": "summary",
                "discharge_prescription": "prescription",
            },
        )
        self.assertEqual(response.status_code, 302)
        plan.refresh_from_db()
        self.assertEqual(plan.planned_discharge_date, date(2026, 10, 15))
        self.assertEqual(plan.status, "scheduled")
        self.assertEqual(plan.actual_discharge_at, actual_discharge_at)

    def test_past_planned_discharge_without_actual_is_not_discharged(self):
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            planned_admission_date=date(2026, 9, 1),
            planned_discharge_date=date(2026, 9, 20),
        )

        self.assertEqual(
            get_patient_admission_status(
                self.patient, as_of=date(2026, 10, 1), treatment_course=self.course_one,
            ),
            "waiting",
        )
        self.assertIsNone(plan.actual_discharge_at)

    def test_actual_discharge_marks_selected_course_discharged(self):
        InpatientPlan.objects.create(
            treatment_course=self.course_one,
            planned_discharge_date=date(2026, 9, 20),
            actual_discharge_at=datetime(2026, 9, 22, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(
            get_patient_admission_status(
                self.patient, as_of=date(2026, 10, 1), treatment_course=self.course_one,
            ),
            "discharged",
        )

    def test_past_planned_admission_without_actual_is_not_inpatient(self):
        InpatientPlan.objects.create(
            treatment_course=self.course_one,
            planned_admission_date=date(2026, 9, 20),
        )

        self.assertEqual(
            get_patient_admission_status(
                self.patient, as_of=date(2026, 10, 1), treatment_course=self.course_one,
            ),
            "waiting",
        )

    def test_course_two_plan_does_not_change_course_one_or_patient_legacy_dates(self):
        self.patient.admission_date = date(2026, 8, 1)
        self.patient.discharge_date = date(2026, 8, 20)
        self.patient.save(update_fields=["admission_date", "discharge_date"])
        self.course_one.admission_date = date(2026, 8, 1)
        self.course_one.discharge_date = date(2026, 8, 20)
        self.course_one.save(update_fields=["admission_date", "discharge_date", "updated_at"])
        InpatientPlan.objects.create(
            treatment_course=self.course_two,
            actual_discharge_at=datetime(2026, 9, 22, 12, tzinfo=timezone.utc),
        )

        self.assertEqual(
            get_patient_admission_status(
                self.patient, as_of=date(2026, 10, 1), treatment_course=self.course_two,
            ),
            "discharged",
        )
        self.assertEqual(
            get_patient_admission_status(
                self.patient, as_of=date(2026, 10, 1), treatment_course=self.course_one,
            ),
            "waiting",
        )
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.admission_date, date(2026, 8, 1))
        self.assertEqual(self.patient.discharge_date, date(2026, 8, 20))

    def test_legacy_past_dates_without_plan_do_not_infer_actual_admission_or_discharge(self):
        self.patient.admission_date = date(2026, 8, 1)
        self.patient.discharge_date = date(2026, 8, 20)
        self.patient.save(update_fields=["admission_date", "discharge_date"])
        self.course_one.admission_date = date(2026, 8, 1)
        self.course_one.discharge_date = date(2026, 8, 20)
        self.course_one.save(update_fields=["admission_date", "discharge_date", "updated_at"])

        self.assertEqual(
            get_patient_admission_status(
                self.patient, as_of=date(2026, 10, 1), treatment_course=self.course_one,
            ),
            "waiting",
        )

    def test_calendar_does_not_count_plan_after_planned_discharge(self):
        InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="scheduled",
            planned_admission_date=date(2026, 9, 1),
            planned_discharge_date=date(2026, 9, 30),
        )
        context = build_inpatient_calendar(year=2026, month=10)
        row = next(row for row in context["calendar_rows"] if row["treatment_course_id"] == self.course_one.pk)
        self.assertEqual(row["timelines"]["inpatient"], [])
        self.assertTrue(all(day["inpatient_count"] == 0 for day in context["days"]))

    def test_inpatient_schedule_prefills_existing_plan_and_assignment(self):
        self.course_two.first_treatment_date = date(2026, 10, 6)
        self.course_two.save(update_fields=["first_treatment_date", "updated_at"])
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_two,
            planned_admission_date=date(2026, 10, 5),
            planned_discharge_date=date(2026, 10, 20),
            estimated_discharge_date=date(2026, 10, 22),
            uncertainty="provisional",
        )
        assignment = ResourceAssignment.objects.create(
            treatment_course=self.course_two,
            resource_pool=self.pool,
            planned_start_date=date(2026, 10, 5),
            planned_end_date=date(2026, 10, 12),
            certainty="provisional",
        )
        response = self.client.get(
            reverse("rtms_app:inpatient_schedule", args=[self.patient.pk]),
            {"course_number": self.course_two.course_number},
        )
        self.assertEqual(response.status_code, 200)
        initial = response.context["form"].initial
        self.assertEqual(initial["admission_date"], plan.planned_admission_date)
        self.assertEqual(initial["treatment_start_date"], self.course_two.first_treatment_date)
        self.assertEqual(initial["room_start_date"], assignment.planned_start_date)
        self.assertEqual(initial["room_end_date"], assignment.planned_end_date)
        self.assertEqual(initial["resource_pool"], self.pool)
        self.assertEqual(initial["certainty"], "provisional")

    def test_waitlist_patient_select_shows_course_status_and_registration_action(self):
        self.course_one.admission_date = date(2026, 10, 5)
        self.course_one.save(update_fields=["admission_date", "updated_at"])
        self.course_two.discharge_date = date(2026, 9, 1)
        self.course_two.save(update_fields=["discharge_date", "updated_at"])
        response = self.client.get(reverse("rtms_app:rtms_waitlist_patient_select"))
        rows = {row["course"].pk: row for row in response.context["course_rows"]}
        self.assertEqual(rows[self.course_one.pk]["status"], "waiting")
        self.assertEqual(rows[self.course_two.pk]["status"], "waiting")
        self.assertContains(response, "待機登録")
        self.assertContains(
            response,
            reverse("rtms_app:rtms_waitlist", args=[self.patient.pk])
            + "?course_number=2",
        )

    def test_admission_capacity_rejects_second_new_admission_when_limit_is_one(self):
        self.capacity.capacity = 1
        self.capacity.save(update_fields=["capacity", "updated_at"])
        confirm_inpatient_schedule(
            treatment_course=self.course_one, resource_pool=self.pool,
            admission_date=date(2026, 10, 5), treatment_start_date=date(2026, 10, 6),
            room_start_date=date(2026, 10, 5), room_end_date=date(2026, 10, 15), user=self.user,
        )
        with self.assertRaises(CapacityConflict):
            confirm_inpatient_schedule(
                treatment_course=self.course_two, resource_pool=self.pool,
                admission_date=date(2026, 10, 5), treatment_start_date=date(2026, 10, 6),
                room_start_date=date(2026, 10, 5), room_end_date=date(2026, 10, 15), user=self.user,
            )
        self.assertEqual(ResourceAssignment.objects.count(), 1)

    def test_waitlist_registration_creates_no_assignment_and_keeps_history(self):
        first = register_waitlist_entry(
            treatment_course=self.course_one, user=self.user,
            status="waiting", preferred_start_note="10月中旬希望", priority=1,
        )
        second = RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="withdrawn", preferred_start_note="11月以降希望", priority=2,
        )
        self.assertEqual(self.course_one.rtms_waitlist_entries.count(), 2)
        self.assertIsNone(first.inpatient_plan_id)
        self.assertIsNone(second.inpatient_plan_id)
        self.assertEqual(ResourceAssignment.objects.count(), 0)

    def test_waitlist_registration_rejects_existing_reserved_statuses(self):
        for index, status in enumerate(("requested", "waiting", "provisional", "scheduled"), start=10):
            with self.subTest(status=status):
                course = TreatmentCourse.objects.create(
                    patient=self.patient, course_number=index,
                )
                RtmSWaitlistEntry.objects.create(treatment_course=course, status=status)
                with self.assertRaises(WaitlistAlreadyRegisteredError):
                    register_waitlist_entry(
                        treatment_course=course, user=self.user, status="waiting",
                    )

    def test_waitlist_registration_is_scoped_to_treatment_course(self):
        first = register_waitlist_entry(
            treatment_course=self.course_one, user=self.user, status="waiting",
        )
        second = register_waitlist_entry(
            treatment_course=self.course_two, user=self.user, status="waiting",
        )
        self.assertEqual(first.treatment_course_id, self.course_one.pk)
        self.assertEqual(second.treatment_course_id, self.course_two.pk)

    def test_waitlist_registration_allows_new_entry_after_cancelled_or_withdrawn(self):
        for index, status in enumerate(("cancelled", "withdrawn"), start=20):
            with self.subTest(status=status):
                course = TreatmentCourse.objects.create(
                    patient=self.patient, course_number=index,
                )
                RtmSWaitlistEntry.objects.create(treatment_course=course, status=status)
                entry = register_waitlist_entry(
                    treatment_course=course, user=self.user, status="waiting",
                )
                self.assertEqual(entry.status, "waiting")

    def test_waitlist_post_shows_duplicate_error(self):
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one, status="waiting",
        )
        response = self.client.post(
            reverse("rtms_app:rtms_waitlist", args=[self.patient.pk]),
            {"course_number": 1, "priority": 0},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "このCourseはすでにrTMS待機リストに登録されています。")
        self.assertEqual(self.course_one.rtms_waitlist_entries.count(), 1)

    def test_waitlist_database_constraint_rejects_reserved_duplicate(self):
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one, status="waiting",
        )
        with self.assertRaises(IntegrityError):
            RtmSWaitlistEntry.objects.create(
                treatment_course=self.course_one, status="scheduled",
            )

    def test_waitlist_entry_becomes_scheduled_only_with_explicit_confirmation(self):
        entry = register_waitlist_entry(
            treatment_course=self.course_one, user=self.user, status="waiting",
        )
        confirm_inpatient_schedule(
            treatment_course=self.course_one, resource_pool=self.pool,
            admission_date=date(2026, 10, 5), treatment_start_date=date(2026, 10, 6),
            room_start_date=date(2026, 10, 5), room_end_date=date(2026, 10, 15),
            user=self.user, waitlist_entry=entry,
        )
        entry.refresh_from_db()
        self.assertEqual(entry.status, "scheduled")
        self.assertIsNotNone(entry.inpatient_plan_id)

    def test_schedule_confirmation_rejects_active_assignment_without_mutation(self):
        original_admission_date = date(2026, 9, 20)
        original_treatment_date = date(2026, 9, 21)
        self.course_one.admission_date = original_admission_date
        self.course_one.first_treatment_date = original_treatment_date
        self.course_one.save(update_fields=["admission_date", "first_treatment_date", "updated_at"])
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="scheduled",
            planned_admission_date=original_admission_date,
        )
        entry = register_waitlist_entry(
            treatment_course=self.course_one, user=self.user, status="waiting",
        )
        actual_start = datetime(2026, 9, 22, 9, 0, tzinfo=timezone.utc)
        actual_end = datetime(2026, 9, 22, 17, 0, tzinfo=timezone.utc)
        assignment = ResourceAssignment.objects.create(
            treatment_course=self.course_one,
            resource_pool=self.pool,
            planned_start_date=date(2026, 9, 22),
            planned_end_date=date(2026, 9, 25),
            actual_start_at=actual_start,
            actual_end_at=actual_end,
            status="active",
            certainty="confirmed",
        )

        with self.assertRaises(ActiveResourceAssignmentError):
            confirm_inpatient_schedule(
                treatment_course=self.course_one,
                resource_pool=self.pool,
                admission_date=date(2026, 10, 5),
                treatment_start_date=date(2026, 10, 6),
                room_start_date=date(2026, 10, 5),
                room_end_date=date(2026, 10, 15),
                user=self.user,
                waitlist_entry=entry,
            )

        self.course_one.refresh_from_db()
        plan.refresh_from_db()
        entry.refresh_from_db()
        assignment.refresh_from_db()
        self.assertEqual(self.course_one.admission_date, original_admission_date)
        self.assertEqual(self.course_one.first_treatment_date, original_treatment_date)
        self.assertEqual(plan.status, "scheduled")
        self.assertEqual(plan.planned_admission_date, original_admission_date)
        self.assertEqual(entry.status, "waiting")
        self.assertIsNone(entry.inpatient_plan_id)
        self.assertEqual(assignment.status, "active")
        self.assertEqual(assignment.actual_start_at, actual_start)
        self.assertEqual(assignment.actual_end_at, actual_end)
        self.assertEqual(ResourceAssignment.objects.filter(treatment_course=self.course_one).count(), 1)

    def test_active_assignment_on_another_pool_prevents_new_planned_assignment(self):
        other_pool = ResourcePool.objects.create(
            code="PHASE2_PRIVATE_OTHER",
            name="別のrTMS個室",
            resource_type="rTMS_private",
            physical_capacity=5,
            operational_target=3,
        )
        ResourceAssignment.objects.create(
            treatment_course=self.course_one,
            resource_pool=self.pool,
            planned_start_date=date(2026, 9, 22),
            status="active",
            certainty="confirmed",
        )

        with self.assertRaises(ActiveResourceAssignmentError):
            confirm_inpatient_schedule(
                treatment_course=self.course_one,
                resource_pool=other_pool,
                admission_date=date(2026, 10, 5),
                treatment_start_date=date(2026, 10, 6),
                room_start_date=date(2026, 10, 5),
                room_end_date=date(2026, 10, 15),
                user=self.user,
            )

        self.assertEqual(ResourceAssignment.objects.filter(treatment_course=self.course_one).count(), 1)
        self.assertEqual(
            ResourceAssignment.objects.get(treatment_course=self.course_one).status,
            "active",
        )
        self.assertFalse(InpatientPlan.objects.filter(treatment_course=self.course_one).exists())

    def test_schedule_confirmation_rejects_waitlist_course_mismatch(self):
        entry = register_waitlist_entry(
            treatment_course=self.course_one, user=self.user, status="waiting",
        )

        with self.assertRaises(InvalidWaitlistEntryError):
            confirm_inpatient_schedule(
                treatment_course=self.course_two,
                resource_pool=self.pool,
                admission_date=date(2026, 10, 5),
                treatment_start_date=date(2026, 10, 6),
                room_start_date=date(2026, 10, 5),
                room_end_date=date(2026, 10, 15),
                user=self.user,
                waitlist_entry=entry,
            )

        self.course_one.refresh_from_db()
        self.course_two.refresh_from_db()
        entry.refresh_from_db()
        self.assertIsNone(self.course_one.admission_date)
        self.assertIsNone(self.course_two.admission_date)
        self.assertFalse(InpatientPlan.objects.filter(
            treatment_course__in=(self.course_one, self.course_two),
        ).exists())
        self.assertEqual(entry.status, "waiting")
        self.assertEqual(ResourceAssignment.objects.count(), 0)

    def test_schedule_confirmation_rejects_ineligible_waitlist_statuses(self):
        entry = register_waitlist_entry(
            treatment_course=self.course_one, user=self.user, status="waiting",
        )

        for status in ("requested", "scheduled", "cancelled", "withdrawn"):
            with self.subTest(status=status):
                entry.status = status
                entry.save(update_fields=["status", "updated_at"])
                with self.assertRaises(InvalidWaitlistEntryError):
                    confirm_inpatient_schedule(
                        treatment_course=self.course_one,
                        resource_pool=self.pool,
                        admission_date=date(2026, 10, 5),
                        treatment_start_date=date(2026, 10, 6),
                        room_start_date=date(2026, 10, 5),
                        room_end_date=date(2026, 10, 15),
                        user=self.user,
                        waitlist_entry=entry,
                    )
                self.assertFalse(InpatientPlan.objects.filter(
                    treatment_course=self.course_one,
                ).exists())
                self.assertEqual(ResourceAssignment.objects.count(), 0)

    def test_waiting_and_provisional_waitlist_entries_can_be_confirmed(self):
        for course, status in (
            (self.course_one, "waiting"),
            (self.course_two, "provisional"),
        ):
            with self.subTest(status=status):
                entry = register_waitlist_entry(
                    treatment_course=course, user=self.user, status=status,
                )
                result = confirm_inpatient_schedule(
                    treatment_course=course,
                    resource_pool=self.pool,
                    admission_date=date(2026, 10, 5),
                    treatment_start_date=date(2026, 10, 6),
                    room_start_date=date(2026, 10, 5),
                    room_end_date=date(2026, 10, 15),
                    user=self.user,
                    waitlist_entry=entry,
                )
                entry.refresh_from_db()
                self.assertEqual(entry.status, "scheduled")
                self.assertEqual(result["assignment"].status, "planned")

    def test_calendar_is_course_scoped_and_preserves_three_timelines(self):
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_one, status="scheduled",
            planned_admission_date=date(2026, 10, 1),
            planned_discharge_date=date(2026, 10, 20),
        )
        ResourceAssignment.objects.create(
            treatment_course=self.course_one, resource_pool=self.pool,
            planned_start_date=date(2026, 10, 5), planned_end_date=date(2026, 10, 10),
        )
        TreatmentSession.objects.create(
            patient=self.patient, treatment_course=self.course_one, course_number=1,
            session_date=date(2026, 10, 6), status="planned",
        )
        TreatmentSession.objects.create(
            patient=self.patient, treatment_course=self.course_two, course_number=2,
            session_date=date(2026, 10, 6), status="planned",
        )
        context = build_inpatient_calendar(year=2026, month=10, treatment_course=self.course_one)
        selected = next(day for day in context["days"] if day["date"] == date(2026, 10, 6))
        self.assertEqual(selected["inpatient_count"], 1)
        self.assertEqual(selected["rtms_count"], 1)
        self.assertEqual(selected["private_room_count"], 1)
        self.assertEqual(plan.treatment_course_id, self.course_one.pk)

    def test_calendar_rows_keep_three_timelines_separate_by_course(self):
        first_plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="scheduled",
            planned_admission_date=date(2026, 10, 2),
            planned_discharge_date=date(2026, 10, 8),
        )
        second_plan = InpatientPlan.objects.create(
            treatment_course=self.course_two,
            status="provisional",
            planned_admission_date=date(2026, 10, 12),
            estimated_discharge_date=date(2026, 10, 18),
        )
        first_assignment = ResourceAssignment.objects.create(
            treatment_course=self.course_one,
            resource_pool=self.pool,
            planned_start_date=date(2026, 10, 3),
            planned_end_date=date(2026, 10, 7),
        )
        second_assignment = ResourceAssignment.objects.create(
            treatment_course=self.course_two,
            resource_pool=self.pool,
            planned_start_date=date(2026, 10, 13),
            planned_end_date=date(2026, 10, 17),
        )
        first_session = TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=self.course_one,
            course_number=1,
            session_date=date(2026, 10, 4),
            status="done",
        )
        second_session = TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=self.course_two,
            course_number=2,
            session_date=date(2026, 10, 14),
            status="planned",
        )

        context = build_inpatient_calendar(year=2026, month=10)
        rows = {row["treatment_course_id"]: row for row in context["calendar_rows"]}

        first_row = rows[self.course_one.pk]
        second_row = rows[self.course_two.pk]
        self.assertEqual(first_row["patient_id"], self.patient.pk)
        self.assertEqual(first_row["course_number"], 1)
        self.assertEqual(first_row["timelines"]["inpatient"][0]["start"], first_plan.planned_admission_date)
        self.assertEqual(first_row["timelines"]["inpatient"][0]["end"], first_plan.planned_discharge_date)
        self.assertEqual(first_row["timelines"]["inpatient"][0]["end_kind"], "planned")
        self.assertEqual(first_row["timelines"]["private_room"][0]["resource_pool"], self.pool)
        self.assertEqual(first_row["timelines"]["private_room"][0]["start"], first_assignment.planned_start_date)
        self.assertEqual(first_row["timelines"]["rtms"][0]["session_id"], first_session.pk)
        self.assertEqual(first_row["timelines"]["rtms"][0]["status"], "done")
        self.assertEqual(second_row["course_number"], 2)
        self.assertEqual(second_row["timelines"]["inpatient"][0]["status"], second_plan.status)
        self.assertEqual(second_row["timelines"]["private_room"][0]["start"], second_assignment.planned_start_date)
        self.assertEqual(second_row["timelines"]["rtms"][0]["session_id"], second_session.pk)
        self.assertNotEqual(
            first_row["timelines"]["rtms"][0]["session_id"],
            second_row["timelines"]["rtms"][0]["session_id"],
        )

    def test_weekly_calendar_covers_eighteen_monday_weeks_and_preserves_course_data(self):
        first_plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="scheduled",
            planned_admission_date=date(2026, 9, 18),
            planned_discharge_date=date(2026, 9, 30),
        )
        second_plan = InpatientPlan.objects.create(
            treatment_course=self.course_two,
            status="provisional",
            planned_admission_date=date(2026, 10, 9),
            planned_discharge_date=date(2026, 10, 20),
        )
        first_assignment = ResourceAssignment.objects.create(
            treatment_course=self.course_one,
            resource_pool=self.pool,
            planned_start_date=date(2026, 9, 19),
            planned_end_date=date(2026, 9, 25),
        )
        first_sessions = [
            TreatmentSession.objects.create(
                patient=self.patient,
                treatment_course=self.course_one,
                course_number=1,
                session_date=session_date,
                status=status,
            )
            for session_date, status in (
                (date(2026, 9, 21), "planned"),
                (date(2026, 9, 22), "done"),
            )
        ]
        second_session = TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=self.course_two,
            course_number=2,
            session_date=date(2026, 10, 12),
            status="skipped",
        )

        context = build_inpatient_calendar_range(
            start_date=date(2026, 9, 16),
            today=date(2026, 9, 16),
        )

        self.assertEqual(context["week_count"], 18)
        self.assertEqual(context["weeks"][0]["start"], date(2026, 9, 14))
        self.assertEqual(context["weeks"][0]["end"], date(2026, 9, 20))
        self.assertTrue(context["weeks"][0]["is_current"])
        self.assertEqual(context["month_spans"][0]["label"], "2026年9月")
        self.assertEqual(context["month_spans"][1]["label"], "2026年10月")
        rows = {row["treatment_course_id"]: row for row in context["weekly_calendar_rows"]}
        first_row = rows[self.course_one.pk]
        second_row = rows[self.course_two.pk]
        self.assertEqual(first_row["timelines"]["inpatient"][0]["start"], first_plan.planned_admission_date)
        self.assertEqual(first_row["timelines"]["private_room"][0]["start"], first_assignment.planned_start_date)
        self.assertEqual(
            {item["session_id"] for item in first_row["timelines"]["rtms"]},
            {session.pk for session in first_sessions},
        )
        self.assertEqual(len(first_row["timelines"]["rtms_period"]), 1)
        period = first_row["timelines"]["rtms_period"][0]
        self.assertEqual(period["start"], date(2026, 9, 21))
        self.assertEqual(period["end"], date(2026, 9, 22))
        self.assertEqual(period["session_count"], 2)
        self.assertEqual(period["planned_count"], 1)
        self.assertEqual(period["done_count"], 1)
        self.assertEqual(second_row["timelines"]["inpatient"][0]["start"], second_plan.planned_admission_date)
        self.assertEqual(second_row["timelines"]["rtms"][0]["session_id"], second_session.pk)
        self.assertEqual(second_row["timelines"]["rtms_period"][0]["skipped_count"], 1)
        self.assertEqual(context["weekly_summary"]["peak_rtms_courses"], 1)
        self.assertIs(context["weeks"], context["weekly_load"])
        self.assertEqual(
            [item["week"] for item in context["weekly_metrics"][0]["weeks"]],
            context["weeks"],
        )

    def test_calendar_shows_service_capacity_and_thirty_day_reservation(self):
        InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="provisional",
            planned_admission_date=date(2026, 10, 2),
            planned_discharge_date=date(2026, 11, 20),
        )
        result = create_or_replace_rtms_treatment_reservation(
            treatment_course=self.course_one,
            admission_date=date(2026, 10, 2),
            first_treatment_date=date(2026, 10, 5),
        )
        TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=self.course_one,
            course_number=1,
            session_date=date(2026, 10, 5),
            status="planned",
        )

        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"week_start": "2026-10-05"},
        )

        self.assertEqual(response.context["week_count"], 12)
        self.assertEqual(len(response.context["weeks"][0]["days"]), 7)
        week = response.context["weeks"][0]
        holidays = get_japanese_holidays(week["start"], week["end"])
        open_days = [day for day in week["days"] if not is_closed(day["date"], holidays)]
        snapshots = [treatment_capacity_snapshot(day["date"]) for day in open_days]
        weekly_capacity = week["treatment_capacity"]
        self.assertEqual(weekly_capacity["operating_day_count"], 5)
        self.assertEqual(
            weekly_capacity["occupied"],
            sum(snapshot["total_occupied"] for snapshot in snapshots),
        )
        self.assertEqual(
            weekly_capacity["capacity"],
            sum(snapshot["total_capacity"] for snapshot in snapshots),
        )
        row = next(
            row for row in response.context["weekly_calendar_rows"]
            if row["treatment_course_id"] == self.course_one.pk
        )
        self.assertEqual(row["reservation_status"], "provisional")
        self.assertEqual(row["schedule_status"], "scheduled")
        self.assertEqual(row["reservation_count"], 30)
        self.assertEqual(row["treatment_end_date"], result["treatment_end_date"])
        self.assertEqual(row["timelines"]["rtms_reservation"][0]["session_count"], 30)
        self.assertEqual(response.context["weeks"][0]["new_course_start_capacity"], 3)
        self.assertContains(response, "3枠")
        self.assertEqual(
            [row["treatment_course_id"] for row in response.context["treatment_calendar_rows"]],
            [self.course_one.pk],
        )
        self.assertNotContains(response, "予定患者")
        self.assertNotContains(response, "A案")
        self.assertNotContains(response, "15回")
        self.assertNotContains(response, "回/日")
        self.assertNotContains(response, "4回/日")
        self.assertNotContains(response, "0/20")
        self.assertContains(response, 'data-admission-start="2026-10-02"')
        self.assertContains(response, 'data-treatment-start="2026-10-05"')
        self.assertContains(response, f'data-treatment-end="{result["treatment_end_date"]:%Y-%m-%d}"')
        self.assertContains(response, 'data-discharge-end="2026-11-20"')
        self.assertContains(response, 'timeline-schedule-segment--admission')
        self.assertContains(response, 'timeline-schedule-segment--treatment')
        self.assertContains(response, 'timeline-schedule-segment--post-treatment')
        self.assertContains(
            response,
            'class="schedule-status schedule-status--scheduled">予定</span>',
        )
        self.assertNotContains(response, "仮予定")
        rendered = response.content.decode()
        self.assertEqual(rendered.count('class="timeline-week-row timeline-week-row--header"'), 1)
        self.assertEqual(rendered.count('class="timeline-week-row timeline-week-row--capacity"'), 2)
        self.assertEqual(rendered.count('class="timeline-week-cell timeline-week-cell--capacity'), 24)
        self.assertEqual(
            rendered.count('class="timeline-track timeline-track--schedule"'),
            len(response.context["weekly_calendar_rows"]),
        )

        self.assertNotIn("capacity-day", rendered)
        self.assertNotIn("timeline-summary", rendered)
        self.assertNotIn('timeline-lane-label">入院予定／実績</div>', rendered)
        self.assertNotIn('timeline-lane-label">入院場所</div>', rendered)
        self.assertNotIn('timeline-lane-label">治療予約</div>', rendered)
        self.assertNotContains(response, "初回日に")

        RtmSTreatmentReservation.objects.filter(
            treatment_course=self.course_one,
            sequence_number=1,
        ).update(status="confirmed")
        with self.assertLogs("rtms_app.services.rtms_inpatient_calendar", level="WARNING"):
            mixed_response = self.client.get(
                reverse("rtms_app:inpatient_calendar"),
                {"week_start": "2026-10-05"},
            )
        self.assertContains(
            mixed_response,
            'class="schedule-status schedule-status--scheduled">予定</span>',
        )
        self.assertNotContains(mixed_response, "要確認")

        RtmSTreatmentReservation.objects.filter(
            treatment_course=self.course_one,
        ).update(status="confirmed")
        confirmed_response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"week_start": "2026-10-05"},
        )
        self.assertContains(confirmed_response, "予定")
        self.assertContains(
            confirmed_response,
            'class="schedule-status schedule-status--scheduled">予定</span>',
        )

    def test_calendar_labels_partially_materialized_treatment_sessions(self):
        self.course_one.admission_date = date(2026, 9, 24)
        self.course_one.first_treatment_date = date(2026, 9, 28)
        self.course_one.save(update_fields=["admission_date", "first_treatment_date", "updated_at"])
        dates = generate_treatment_dates(date(2026, 9, 28), total=6)
        TreatmentSession.objects.bulk_create([
            TreatmentSession(
                patient=self.patient,
                treatment_course=self.course_one,
                course_number=1,
                session_date=treatment_date,
                status="planned",
            )
            for treatment_date in dates
        ])

        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"week_start": "2026-09-28"},
        )

        row = next(
            row for row in response.context["weekly_calendar_rows"]
            if row["treatment_course_id"] == self.course_one.pk
        )
        self.assertEqual(row["materialized_session_count"], 6)
        self.assertEqual(row["planned_treatment_sessions"], 30)
        self.assertContains(response, "rTMS 6/30")
        self.assertNotContains(response, "rTMS 30/30")

    def test_calendar_composes_course_dates_with_one_six_or_thirty_sessions(self):
        first_treatment_date = date(2026, 10, 5)
        treatment_dates = generate_course_treatment_dates(first_treatment_date)
        courses_by_session_count = {}
        for course_number, session_count in enumerate((1, 6, 30), start=3):
            course = TreatmentCourse.objects.create(
                patient=self.patient,
                course_number=course_number,
                admission_date=date(2026, 10, 2),
                first_treatment_date=first_treatment_date,
            )
            TreatmentSession.objects.bulk_create([
                TreatmentSession(
                    patient=self.patient,
                    treatment_course=course,
                    course_number=course_number,
                    session_date=treatment_date,
                    status="planned",
                )
                for treatment_date in treatment_dates[:session_count]
            ])
            courses_by_session_count[session_count] = course

        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"week_start": first_treatment_date.isoformat()},
        )

        rows = {
            row["treatment_course_id"]: row
            for row in response.context["weekly_calendar_rows"]
        }
        for session_count, course in courses_by_session_count.items():
            with self.subTest(session_count=session_count):
                row = rows[course.pk]
                session_dates = {
                    item["date"] for item in row["timelines"]["rtms"]
                }
                self.assertEqual(
                    row["rtms_planned_dates"],
                    treatment_dates[session_count:],
                )
                effective_dates = session_dates | set(row["rtms_planned_dates"])
                self.assertEqual(effective_dates, set(treatment_dates))
                self.assertEqual(len(effective_dates), 30)
                track = next(
                    line for line in response.content.decode().splitlines()
                    if f'data-course-id="{course.pk}"' in line
                    and 'data-timeline-type="patient_schedule"' in line
                )
                self.assertIn('data-treatment-end="2026-11-17"', track)

    def test_calendar_deduplicates_reservation_session_and_derived_dates(self):
        first_treatment_date = date(2026, 10, 5)
        treatment_dates = generate_course_treatment_dates(first_treatment_date)
        reserved_course = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=6,
            admission_date=date(2026, 10, 2),
            first_treatment_date=first_treatment_date,
        )
        RtmSTreatmentReservation.objects.create(
            treatment_course=reserved_course,
            treatment_date=treatment_dates[0],
            sequence_number=1,
            status="provisional",
            capacity_type="rTMS_private",
        )
        paired_course = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=7,
            admission_date=date(2026, 10, 2),
            first_treatment_date=first_treatment_date,
        )
        TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=paired_course,
            course_number=paired_course.course_number,
            session_date=treatment_dates[0],
            status="planned",
        )
        RtmSTreatmentReservation.objects.create(
            treatment_course=paired_course,
            treatment_date=treatment_dates[0],
            sequence_number=1,
            status="provisional",
            capacity_type="rTMS_private",
        )
        skipped_course = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=8,
            admission_date=date(2026, 10, 2),
            first_treatment_date=first_treatment_date,
        )
        skipped_session = TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=skipped_course,
            course_number=skipped_course.course_number,
            session_date=treatment_dates[0],
            status="skipped",
        )

        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"week_start": first_treatment_date.isoformat()},
        )

        rows = {
            row["treatment_course_id"]: row
            for row in response.context["weekly_calendar_rows"]
        }
        reserved_row = rows[reserved_course.pk]
        self.assertEqual(reserved_row["rtms_planned_dates"], treatment_dates[1:])
        self.assertEqual(
            {reserved_row["reservation_start"], *reserved_row["rtms_planned_dates"]},
            set(treatment_dates),
        )
        paired_row = rows[paired_course.pk]
        self.assertEqual(paired_row["rtms_planned_dates"], treatment_dates[1:])
        self.assertEqual(
            {item["date"] for item in paired_row["timelines"]["rtms"]}
            | set(paired_row["rtms_planned_dates"]),
            set(treatment_dates),
        )
        skipped_row = rows[skipped_course.pk]
        self.assertEqual(skipped_row["timelines"]["rtms"][0]["session_id"], skipped_session.pk)
        self.assertEqual(skipped_row["rtms_planned_dates"], treatment_dates[1:])
        self.assertEqual(
            {item["date"] for item in skipped_row["timelines"]["rtms"]}
            | set(skipped_row["rtms_planned_dates"]),
            set(treatment_dates),
        )

    def test_calendar_new_course_capacity_uses_configured_daily_capacity(self):
        private_capacity = RtmSTreatmentCapacity.objects.get(
            capacity_type="rTMS_private",
            valid_from=date(2026, 1, 1),
        )
        general_capacity = RtmSTreatmentCapacity.objects.get(
            capacity_type="general_ward",
            valid_from=date(2026, 1, 1),
        )
        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"week_start": "2026-10-05"},
        )

        week_capacity = response.context["weeks"][0]["treatment_capacity"]
        self.assertEqual(week_capacity["daily_capacity"], 4)
        self.assertEqual(week_capacity["daily_capacity_breakdown"], (3, 1))
        self.assertEqual(response.context["weeks"][0]["new_course_start_capacity"], 4)
        self.assertContains(response, "4枠")
        self.assertNotContains(response, "予定患者")
        self.assertNotContains(response, "A案")
        self.assertNotContains(response, "0/20")
        self.assertNotContains(response, "3/20")

        general_capacity.capacity = 2
        general_capacity.save(update_fields=["capacity", "updated_at"])
        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"week_start": "2026-10-05"},
        )
        self.assertEqual(
            response.context["weeks"][0]["treatment_capacity"]["daily_capacity"],
            private_capacity.capacity + general_capacity.capacity,
        )
        self.assertEqual(response.context["weeks"][0]["treatment_capacity"]["capacity"], 25)
        self.assertEqual(response.context["weeks"][0]["new_course_start_capacity"], 5)
        self.assertContains(response, "5枠")
        self.assertNotContains(response, "回/日")

    def test_calendar_displays_past_course_and_session_without_capacity_configuration(self):
        RtmSTreatmentCapacity.objects.all().delete()
        treatment_date = date(2025, 12, 22)
        self.course_one.admission_date = date(2025, 12, 19)
        self.course_one.first_treatment_date = treatment_date
        self.course_one.save(update_fields=[
            "admission_date", "first_treatment_date", "updated_at",
        ])
        session = TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=self.course_one,
            course_number=self.course_one.course_number,
            session_date=treatment_date,
            status="planned",
        )

        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"week_start": treatment_date.isoformat()},
        )

        self.assertEqual(response.status_code, 200)
        row = next(
            row for row in response.context["weekly_calendar_rows"]
            if row["treatment_course_id"] == self.course_one.pk
        )
        self.assertEqual(row["timelines"]["rtms"][0]["session_id"], session.pk)
        first_week = response.context["weeks"][0]
        self.assertIsNone(first_week["new_course_start_capacity"])
        self.assertIsNone(first_week["days"][0]["private_capacity"]["capacity"])
        self.assertIsNone(first_week["days"][0]["total_capacity"])
        self.assertIsNone(first_week["treatment_capacity"]["capacity"])
        self.assertContains(response, "—")

        year_end_response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"week_start": "2025-12-29"},
        )
        self.assertEqual(year_end_response.status_code, 200)
        self.assertIsNone(year_end_response.context["weeks"][0]["days"][0]["total_capacity"])

    def test_calendar_weekly_counts_scheduled_courses_not_sessions(self):
        for session_number in range(5):
            TreatmentSession.objects.create(
                patient=self.patient,
                treatment_course=self.course_one,
                course_number=1,
                session_date=date(2026, 10, 5) + timedelta(days=session_number),
                status="planned",
            )
        for session_number, session_date in enumerate(
            (date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)),
            start=1,
        ):
            TreatmentSession.objects.create(
                patient=self.patient,
                treatment_course=self.course_two,
                course_number=2,
                session_date=session_date,
                status="planned",
            )
            RtmSTreatmentReservation.objects.create(
                treatment_course=self.course_two,
                treatment_date=session_date,
                sequence_number=session_number,
                status="provisional",
                capacity_type="rTMS_private",
            )

        fallback_courses = [
            TreatmentCourse.objects.create(patient=self.patient, course_number=course_number)
            for course_number in range(3, 7)
        ]
        for course in fallback_courses:
            course.first_treatment_date = date(2026, 10, 19)
            course.save(update_fields=["first_treatment_date", "updated_at"])

        skipped_course = TreatmentCourse.objects.create(patient=self.patient, course_number=7)
        TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=skipped_course,
            course_number=7,
            session_date=date(2026, 10, 12),
            status="skipped",
        )

        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"week_start": "2026-09-28"},
        )
        weeks = {
            week["start"]: week["rtms_course_count"]
            for week in response.context["weeks"]
        }

        self.assertEqual(weeks[date(2026, 9, 28)], 0)
        self.assertEqual(weeks[date(2026, 10, 5)], 2)
        self.assertEqual(weeks[date(2026, 10, 12)], 0)
        self.assertEqual(weeks[date(2026, 10, 19)], 4)
        self.assertNotContains(response, "予定患者")
        self.assertNotContains(response, "回/日")
        self.assertNotContains(response, "残り")

    def test_weekly_course_count_deduplicates_reservation_and_fallback_dates(self):
        treatment_date = date(2026, 10, 5)
        reservation = SimpleNamespace(
            treatment_course_id=self.course_one.pk,
            treatment_date=treatment_date,
        )
        weeks = [{"start": treatment_date, "end": treatment_date + timedelta(days=6)}]
        rows = [{
            "treatment_course_id": self.course_one.pk,
            "rtms_planned_dates": [treatment_date, treatment_date + timedelta(days=1)],
        }]

        counts = _weekly_rtms_course_counts(weeks, [reservation], [], rows)

        self.assertEqual(counts[treatment_date], 1)

    def test_calendar_shows_independent_new_course_capacity_for_each_week(self):
        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"week_start": "2026-10-05"},
        )

        weeks = response.context["weeks"]
        self.assertEqual(len(weeks), 12)
        self.assertEqual(weeks[0]["new_course_start_capacity"], 4)
        self.assertEqual(weeks[0]["rtms_course_count"], 0)
        self.assertContains(response, "週別rTMS新規開始可能枠")
        self.assertNotContains(response, "A案")
        self.assertNotContains(response, "予定患者")
        self.assertNotContains(response, "30回コース開始の目安")
        rendered = response.content.decode()
        self.assertEqual(rendered.count('class="timeline-week-row timeline-week-row--capacity"'), 2)
        self.assertEqual(rendered.count('class="timeline-week-cell timeline-week-cell--capacity'), 24)
        self.assertEqual(rendered.count("<strong>4枠</strong>"), 24)

    def test_calendar_a_capacity_checks_reservations_beyond_twelve_week_window(self):
        range_start = date(2026, 9, 7)
        final_week_start = range_start + timedelta(weeks=11)
        holidays = get_japanese_holidays(
            final_week_start,
            final_week_start + timedelta(days=365),
        )
        last_treatment_date = generate_treatment_dates(
            final_week_start,
            total=30,
            holidays=holidays,
        )[-1]
        reservations = []
        for index, capacity_type in enumerate(
            ("rTMS_private", "rTMS_private", "rTMS_private", "general_ward"),
            start=1,
        ):
            course = TreatmentCourse.objects.create(
                patient=self.patient,
                course_number=index + 2,
            )
            reservations.append(RtmSTreatmentReservation(
                treatment_course=course,
                treatment_date=last_treatment_date,
                sequence_number=30,
                status="confirmed",
                capacity_type=capacity_type,
            ))
        RtmSTreatmentReservation.objects.bulk_create(reservations)

        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"week_start": range_start.isoformat()},
        )

        final_week = next(
            week for week in response.context["weeks"]
            if week["start"] == final_week_start
        )
        self.assertGreater(last_treatment_date, final_week["end"])
        self.assertEqual(final_week["new_course_start_capacity"], 0)

    def test_calendar_navigation_supports_week_and_twelve_week_steps(self):
        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"week_start": "2026-10-05"},
        )

        self.assertEqual(response.context["week_count"], 12)
        self.assertIn("week_start=2026-09-28", response.context["previous_week_url"])
        self.assertIn("week_start=2026-07-13", response.context["previous_range_url"])
        self.assertIn("week_start=2026-10-12", response.context["next_week_url"])
        self.assertIn("week_start=2026-12-28", response.context["next_range_url"])
        self.assertContains(response, 'aria-label="1週前"')
        self.assertContains(response, 'aria-label="1週後"')
        self.assertContains(response, "前の12週")
        self.assertContains(response, "次の12週")
        self.assertContains(response, "今日へ")
        self.assertContains(response, f'href="{response.context["today_range_url"]}"')
        rendered = response.content.decode()
        button_positions = [
            rendered.index('aria-label="前の12週間"'),
            rendered.index('aria-label="1週前"'),
            rendered.index(">今日へ</a>"),
            rendered.index('aria-label="1週後"'),
            rendered.index('aria-label="次の12週間"'),
        ]
        self.assertEqual(button_positions, sorted(button_positions))

    def test_calendar_waiting_status_uses_missing_dates_not_waitlist_status(self):
        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"week_start": "2026-10-05"},
        )

        row = next(
            row for row in response.context["weekly_calendar_rows"]
            if row["treatment_course_id"] == self.course_one.pk
        )
        self.assertEqual(row["schedule_status"], "waiting")
        self.assertContains(
            response,
            'class="schedule-status schedule-status--waiting">待機</span>',
        )
        self.assertNotContains(response, "未予約")

    def test_calendar_completed_status_uses_admission_procedure_flag(self):
        self.course_one.admission_date = date(2026, 10, 5)
        self.course_one.first_treatment_date = date(2026, 10, 6)
        self.course_one.is_admission_procedure_done = True
        self.course_one.save(update_fields=[
            "admission_date",
            "first_treatment_date",
            "is_admission_procedure_done",
            "updated_at",
        ])

        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"week_start": "2026-10-05"},
        )

        row = next(
            row for row in response.context["weekly_calendar_rows"]
            if row["treatment_course_id"] == self.course_one.pk
        )
        self.assertEqual(row["schedule_status"], "completed")
        self.assertContains(response, "確定（実施済）")
        self.assertNotContains(response, "未予約")
        self.assertNotContains(response, "仮予定")

    def test_calendar_completed_status_uses_actual_admission_or_done_session(self):
        self.course_one.admission_date = date(2026, 10, 5)
        self.course_one.first_treatment_date = date(2026, 10, 6)
        self.course_one.save(update_fields=["admission_date", "first_treatment_date", "updated_at"])
        InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="admitted",
            planned_admission_date=date(2026, 10, 5),
            actual_admission_at=datetime(2026, 10, 2, 9, tzinfo=timezone.utc),
        )
        TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=self.course_two,
            course_number=2,
            session_date=date(2026, 10, 6),
            status="done",
        )
        self.course_two.admission_date = date(2026, 10, 5)
        self.course_two.first_treatment_date = date(2026, 10, 6)
        self.course_two.save(update_fields=["admission_date", "first_treatment_date", "updated_at"])

        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"week_start": "2026-10-05"},
        )

        statuses = {
            row["treatment_course_id"]: row["schedule_status"]
            for row in response.context["weekly_calendar_rows"]
        }
        self.assertEqual(statuses[self.course_one.pk], "completed")
        self.assertEqual(statuses[self.course_two.pk], "completed")

    def test_calendar_patient_rows_omit_inpatient_location_actions(self):
        self.course_one.admission_date = date(2026, 10, 5)
        self.course_one.first_treatment_date = date(2026, 10, 6)
        self.course_one.save(update_fields=["admission_date", "first_treatment_date", "updated_at"])

        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"week_start": "2026-10-05"},
        )

        self.assertNotContains(response, "入院場所")
        self.assertNotContains(response, "inpatient/schedule/")
        self.assertNotContains(response, "予定変更")

    def test_calendar_without_reservations_uses_generated_thirty_date_period(self):
        self.course_one.first_visit_date = date(2026, 9, 10)
        self.course_one.admission_date = date(2026, 9, 11)
        self.course_one.first_treatment_date = date(2026, 9, 14)
        self.course_one.save(update_fields=[
            "first_visit_date",
            "admission_date",
            "first_treatment_date",
            "updated_at",
        ])

        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"week_start": "2026-09-28"},
        )

        row = next(
            row for row in response.context["weekly_calendar_rows"]
            if row["treatment_course_id"] == self.course_one.pk
        )
        self.assertEqual(row["reservation_count"], 0)
        self.assertEqual(row["timelines"]["rtms_period"], [])
        self.assertEqual(len(row["rtms_planned_dates"]), 30)
        self.assertEqual(
            row["rtms_planned_period"]["start"], date(2026, 9, 14),
        )
        self.assertEqual(
            row["rtms_planned_period"]["end"], date(2026, 10, 29),
        )
        self.assertEqual(
            row["rtms_planned_period"]["visible_start"], date(2026, 9, 28),
        )
        self.assertEqual(
            row["rtms_planned_period"]["visible_end"], date(2026, 10, 29),
        )
        self.assertContains(response, 'data-admission-start="2026-09-11"')
        self.assertContains(response, 'data-treatment-start="2026-09-14"')
        self.assertContains(response, 'data-treatment-end="2026-10-29"')
        self.assertContains(response, 'data-discharge-end=""')
        self.assertContains(response, "予定")
        self.assertContains(
            response,
            'class="schedule-status schedule-status--scheduled">予定</span>',
        )
        self.assertEqual(
            RtmSTreatmentReservation.objects.filter(
                treatment_course=self.course_one,
            ).count(),
            0,
        )

    def test_weekly_capacity_excludes_holidays_and_year_end_closures(self):
        context = build_inpatient_calendar_range(
            start_date=date(2026, 10, 12),
            today=date(2026, 10, 3),
        )
        holiday_week = context["weeks"][0]["treatment_capacity"]
        year_end_week = next(
            week["treatment_capacity"]
            for week in context["weeks"]
            if week["start"] == date(2026, 12, 28)
        )
        self.assertEqual(holiday_week["operating_day_count"], 4)
        self.assertEqual(holiday_week["capacity"], 16)
        self.assertEqual(holiday_week["private"]["capacity"], 12)
        self.assertEqual(holiday_week["general"]["capacity"], 4)
        self.assertEqual(year_end_week["operating_day_count"], 1)
        self.assertEqual(year_end_week["capacity"], 4)

    def test_reservation_preview_reports_full_dates_with_capacity_details(self):
        courses = [self.course_one]
        for index in range(4):
            patient = Patient.objects.create(
                card_id=f"8270{index + 1}",
                name=f"Preview patient {index + 1}",
                birth_date=date(1980, 1, 1),
            )
            courses.append(TreatmentCourse.objects.create(patient=patient, course_number=1))
        for course in courses[:4]:
            create_or_replace_rtms_treatment_reservation(
                treatment_course=course,
                admission_date=date(2026, 10, 5),
                first_treatment_date=date(2026, 10, 5),
            )

        response = self.client.get(
            reverse("rtms_app:rtms_reservation_preview"),
            {
                "treatment_course_id": courses[4].pk,
                "first_treatment_date": "2026-10-05",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["treatment_count"], 30)
        self.assertEqual(payload["conflicts"][0]["date"], "2026-10-05")
        self.assertEqual(payload["conflicts"][0]["total_occupied"], 4)
        self.assertEqual(payload["conflicts"][0]["total_capacity"], 4)
        self.assertEqual(RtmSTreatmentReservation.objects.count(), 120)

    def test_calendar_without_new_booking_button_preserves_waitlist_scheduling(self):
        entry = RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="waiting",
            priority=2,
            preferred_start_note="10月中旬",
            comment="退院調整後",
        )
        response = self.client.get(reverse("rtms_app:inpatient_calendar"))
        self.assertContains(response, "待機患者")
        self.assertContains(response, "退院調整後")
        self.assertContains(response, "日程設定")
        self.assertNotContains(response, "新規予約")
        self.assertContains(response, 'id="reservationForm"')
        self.assertContains(response, 'id="bookingPanelMount"')
        self.assertContains(response, 'data-booking-open data-idle-label="日程設定"')
        self.assertContains(response, 'name="action" value="save_calendar_course"')

        response = self.client.post(
            reverse("rtms_app:inpatient_calendar"),
            {
                "action": "save_calendar_course",
                "treatment_course_id": self.course_two.pk,
                "admission_date": "2026-10-05",
                "first_treatment_date": "2026-10-06",
                "planned_treatment_sessions": "30",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            RtmSTreatmentReservation.objects.filter(
                treatment_course=self.course_two,
                status="provisional",
            ).count(),
            30,
        )

        response = self.client.post(
            reverse("rtms_app:inpatient_calendar"),
            {
                "action": "save_calendar_course",
                "treatment_course_id": self.course_one.pk,
                "waitlist_entry_id": entry.pk,
                "admission_date": "2026-10-05",
                "first_treatment_date": "2026-10-06",
                "planned_treatment_sessions": "30",
                "preferred_start_note": "10月上旬へ変更",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            RtmSTreatmentReservation.objects.filter(
                treatment_course=self.course_one,
                status="provisional",
            ).count(),
            30,
        )
        entry.refresh_from_db()
        self.assertEqual(entry.status, "provisional")
        self.assertEqual(entry.preferred_start_note, "10月上旬へ変更")

    def test_inpatient_schedule_confirmation_confirms_waitlist_reservations(self):
        entry = RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="provisional",
            priority=1,
        )
        create_or_replace_rtms_treatment_reservation(
            treatment_course=self.course_one,
            admission_date=date(2026, 10, 5),
            first_treatment_date=date(2026, 10, 6),
        )

        response = self.client.post(
            reverse("rtms_app:inpatient_schedule", args=[self.patient.pk]),
            {
                "course_number": self.course_one.course_number,
                "admission_date": "2026-10-05",
                "treatment_start_date": "2026-10-06",
                "room_start_date": "2026-10-05",
                "room_end_date": "2026-10-15",
                "certainty": "confirmed",
                "resource_pool": self.pool.pk,
            },
        )

        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.status, "scheduled")
        self.assertEqual(
            RtmSTreatmentReservation.objects.filter(
                treatment_course=self.course_one,
                status="confirmed",
            ).count(),
            30,
        )

    def test_calendar_course_adjustment_updates_course_and_inpatient_plan(self):
        self.course_one.private_room_planned = True
        self.course_one.save(update_fields=["private_room_planned", "updated_at"])
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="waiting",
            priority=1,
        )
        response = self.client.post(
            reverse("rtms_app:inpatient_calendar"),
            {
                "action": "save_calendar_course",
                "treatment_course_id": str(self.course_one.pk),
                "admission_date": "2026-10-05",
                "first_treatment_date": "2026-10-06",
                "is_all_case_survey": "on",
                "year": "2026",
                "month": "10",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.course_one.refresh_from_db()
        self.course_two.refresh_from_db()
        self.assertEqual(self.course_one.admission_date, date(2026, 10, 5))
        self.assertEqual(self.course_one.first_treatment_date, date(2026, 10, 6))
        self.assertTrue(self.course_one.private_room_planned)
        self.assertTrue(self.course_one.is_all_case_survey)
        self.assertFalse(self.course_two.private_room_planned)
        self.assertFalse(self.course_two.is_all_case_survey)
        plan = InpatientPlan.objects.get(treatment_course=self.course_one)
        self.assertEqual(plan.planned_admission_date, date(2026, 10, 5))
        self.assertEqual(ResourceAssignment.objects.count(), 0)

        calendar = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"year": 2026, "month": 10},
        )
        calendar_row = next(
            row for row in calendar.context["weekly_calendar_rows"]
            if row["treatment_course_id"] == self.course_one.pk
        )
        self.assertEqual(calendar_row["reservation_start"], date(2026, 10, 6))
        self.assertEqual(calendar_row["reservation_count"], 30)
        self.assertContains(calendar, 'data-treatment-start="2026-10-06"')
        self.assertContains(
            calendar,
            f'data-treatment-end="{calendar_row["treatment_end_date"]:%Y-%m-%d}"',
        )
        self.assertNotContains(calendar, "入院場所")
        self.assertContains(calendar, "全例調査対象")
        self.assertNotContains(calendar, "予定変更")
        self.assertNotContains(calendar, "inpatient/schedule/")
        self.assertNotContains(calendar, "course-adjustment-form")

    def test_weekly_start_options_only_enable_weeks_with_capacity(self):
        weeks = [
            {
                "start": date(2026, 10, 5),
                "end": date(2026, 10, 11),
                "new_course_start_capacity": 2,
            },
            {
                "start": date(2026, 10, 12),
                "end": date(2026, 10, 18),
                "new_course_start_capacity": 0,
            },
        ]

        add_new_course_start_options(weeks)

        self.assertTrue(weeks[0]["selectable"])
        self.assertEqual(weeks[0]["first_treatment_date"], date(2026, 10, 5))
        self.assertEqual(weeks[0]["admission_date"], date(2026, 10, 2))
        self.assertFalse(weeks[1]["selectable"])

    def test_waitlist_calendar_selection_starts_with_disabled_capacity_slots(self):
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="waiting",
        )
        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"week_start": "2026-10-05"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "calendar-scheduling-panel")
        self.assertContains(response, 'id="bookingPanelMount"')
        self.assertContains(response, "data-booking-open")
        self.assertContains(response, 'data-patient-name="Phase Two"')
        self.assertContains(response, 'data-course-number="1"')
        self.assertContains(response, "(row || courseGroup).after(panel)")
        self.assertContains(response, "waitlist-compact-entry--active")
        self.assertContains(response, "data-first-treatment-date")
        self.assertContains(response, "data-admission-date")
        self.assertContains(response, 'name="action" value="save_calendar_course"')
        self.assertContains(response, 'name="waitlist_entry_id"')
        self.assertContains(response, 'type="date" name="admission_date"')
        self.assertContains(response, 'type="date" name="first_treatment_date"')
        self.assertContains(response, 'aria-pressed="false" disabled aria-disabled="true"')
        self.assertNotContains(response, 'id="reservationModal"')
        self.assertEqual(response.content.count(b">10/5\xe9\x80\xb1</span>"), 1)
        self.assertContains(response, "上のカレンダーから希望する開始週を選択してください")

    def test_provisional_course_has_gantt_edit_action_only(self):
        provisional = RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="provisional",
        )
        waiting = RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_two,
            status="waiting",
        )
        scheduled_course = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=4,
            admission_date=date(2026, 10, 2),
            first_treatment_date=date(2026, 10, 5),
        )
        RtmSWaitlistEntry.objects.create(
            treatment_course=scheduled_course,
            status="scheduled",
        )
        requested = RtmSWaitlistEntry.objects.create(
            treatment_course=TreatmentCourse.objects.create(
                patient=self.patient,
                course_number=3,
            ),
            status="requested",
        )
        self.course_one.admission_date = date(2026, 10, 2)
        self.course_one.first_treatment_date = date(2026, 10, 5)
        self.course_one.save(update_fields=["admission_date", "first_treatment_date", "updated_at"])
        self.course_two.admission_date = date(2026, 10, 2)
        self.course_two.first_treatment_date = date(2026, 10, 5)
        self.course_two.save(update_fields=["admission_date", "first_treatment_date", "updated_at"])

        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"week_start": "2026-10-05"},
        )

        self.assertEqual(response.status_code, 200)
        rows = {
            row["treatment_course_id"]: row
            for row in response.context["treatment_calendar_rows"]
        }
        self.assertEqual(rows[self.course_one.pk]["provisional_waitlist_entry"], provisional)
        self.assertIsNone(rows[self.course_two.pk]["provisional_waitlist_entry"])
        self.assertIsNone(rows[scheduled_course.pk]["provisional_waitlist_entry"])
        self.assertContains(response, "日程調整中")
        provisional_actions = re.findall(
            rb"<button\b[^>]*\bdata-provisional-edit(?:\s|=|>)[^>]*>",
            response.content,
            flags=re.DOTALL,
        )
        self.assertEqual(len(provisional_actions), 1)
        self.assertContains(response, 'data-waitlist-id="%s"' % provisional.pk)
        self.assertContains(response, 'data-admission-date="2026-10-02"')
        self.assertContains(response, 'data-first-treatment-date="2026-10-05"')
        self.assertCountEqual(response.context["waitlist_entries"], [waiting, requested])
        self.assertEqual(response.context["waitlist_display_count"], 2)

        management_response = self.client.get(reverse("rtms_app:inpatient_waitlist"))
        self.assertCountEqual(management_response.context["entries"], [waiting, requested])

    def test_waitlist_display_filters_provisional_by_course(self):
        for course in (self.course_one, self.course_two):
            course.admission_date = date(2026, 10, 2)
            course.first_treatment_date = date(2026, 10, 5)
            course.save(update_fields=["admission_date", "first_treatment_date", "updated_at"])
        waiting = RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="waiting",
        )
        provisional = RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_two,
            status="provisional",
        )

        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"week_start": "2026-10-05"},
        )

        self.assertCountEqual(response.context["waitlist_entries"], [waiting])
        self.assertEqual(response.context["waitlist_display_count"], 1)
        rows = {
            row["treatment_course_id"]: row
            for row in response.context["treatment_calendar_rows"]
        }
        self.assertIsNone(rows[self.course_one.pk]["provisional_waitlist_entry"])
        self.assertEqual(rows[self.course_two.pk]["provisional_waitlist_entry"], provisional)
        provisional_actions = re.findall(
            rb"<button\b[^>]*\bdata-provisional-edit(?:\s|=|>)[^>]*>",
            response.content,
            flags=re.DOTALL,
        )
        self.assertEqual(len(provisional_actions), 1)
        self.assertIn(f'data-course-id="{self.course_two.pk}"'.encode(), provisional_actions[0])

        management_response = self.client.get(reverse("rtms_app:inpatient_waitlist"))
        self.assertEqual(list(management_response.context["entries"]), [waiting])

    def test_first_visit_week_selection_remains_enabled(self):
        response = self.client.get(
            reverse("rtms_app:patient_first_visit", args=[self.patient.pk]),
            {"course_number": 1},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "data-first-treatment-date")
        self.assertNotContains(response, 'aria-pressed="false" disabled aria-disabled="true"')
        self.assertGreaterEqual(response.content.count(b">10/5\xe9\x80\xb1</span>"), 2)

    def test_weekly_summary_counts_courses_not_sessions(self):
        for session_date in (date(2026, 9, 21), date(2026, 9, 23)):
            TreatmentSession.objects.create(
                patient=self.patient,
                treatment_course=self.course_one,
                course_number=1,
                session_date=session_date,
                status="planned",
            )
        context = build_inpatient_calendar_range(
            start_date=date(2026, 9, 16),
            today=date(2026, 9, 16),
        )
        treatment_week = context["weeks"][1]
        self.assertEqual(treatment_week["rtms_course_count"], 1)
        self.assertEqual(context["weekly_metrics"][2]["weeks"][1]["value"], 1)

    def test_monthly_private_room_fallback_is_course_scoped_and_assignment_wins(self):
        self.course_one.private_room_planned = True
        self.course_one.save(update_fields=["private_room_planned", "updated_at"])
        InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="scheduled",
            planned_admission_date=date(2026, 10, 5),
            planned_discharge_date=date(2026, 10, 10),
        )
        InpatientPlan.objects.create(
            treatment_course=self.course_two,
            status="scheduled",
            planned_admission_date=date(2026, 10, 5),
            planned_discharge_date=date(2026, 10, 10),
        )
        context = build_inpatient_calendar(year=2026, month=10)
        rows = {row["treatment_course_id"]: row for row in context["calendar_rows"]}
        first_room = rows[self.course_one.pk]["timelines"]["private_room"]
        second_room = rows[self.course_two.pk]["timelines"]["private_room"]
        self.assertEqual(len(first_room), 1)
        self.assertTrue(first_room[0]["planned_only"])
        self.assertEqual(second_room, [])
        day = next(day for day in context["days"] if day["date"] == date(2026, 10, 6))
        self.assertEqual(day["private_room_count"], 1)

        assignment = ResourceAssignment.objects.create(
            treatment_course=self.course_one,
            resource_pool=self.pool,
            planned_start_date=date(2026, 10, 7),
            planned_end_date=date(2026, 10, 8),
        )
        assigned_context = build_inpatient_calendar(year=2026, month=10)
        assigned_rows = {
            row["treatment_course_id"]: row
            for row in assigned_context["calendar_rows"]
        }
        assigned_room = assigned_rows[self.course_one.pk]["timelines"]["private_room"]
        self.assertEqual(len(assigned_room), 1)
        self.assertFalse(assigned_room[0]["planned_only"])
        self.assertEqual(assigned_room[0]["assignment"].pk, assignment.pk)

    def test_calendar_template_renders_timeline_context_for_selected_course(self):
        InpatientPlan.objects.create(
            treatment_course=self.course_two,
            status="scheduled",
            planned_admission_date=date(2026, 10, 1),
            planned_discharge_date=date(2026, 10, 3),
        )
        TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=self.course_two,
            course_number=2,
            session_date=date(2026, 10, 2),
            status="planned",
        )

        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {
                "patient_id": self.patient.pk,
                "course_number": 2,
                "year": 2026,
                "month": 10,
            },
        )

        self.assertContains(response, self.patient.name)
        self.assertContains(response, "Course #2")
        self.assertContains(response, "待機")
        self.assertContains(
            response,
            'class="schedule-status schedule-status--waiting">待機</span>',
        )
        self.assertContains(response, 'class="timeline-week-row timeline-week-row--header"')
        self.assertContains(response, 'class="timeline-board" style="--timeline-weeks: 12;"')
        self.assertNotContains(response, 'class="calendar-details')
        self.assertContains(response, 'data-timeline-type="patient_schedule"')
        self.assertContains(response, 'data-course-number="2"')
        self.assertContains(response, 'data-treatment-start="2026-10-02"')
        self.assertContains(response, 'data-treatment-end="2026-10-02"')
        self.assertContains(response, "患者ガントチャート")
        rendered = response.content.decode()
        visible_calendar = rendered.split('<div class="modal fade" id="reservationModal"', 1)[0]
        timeline_html = visible_calendar.split('<section class="inpatient-timeline', 1)[1].split(
            '<section class="calendar-section', 1
        )[0]
        self.assertNotIn("Patient ID", timeline_html)
        self.assertNotIn("TreatmentSession", timeline_html)
        self.assertNotIn("入院場所:", timeline_html)
        self.assertContains(response, "待機")
        self.assertLess(
            rendered.index('class="timeline-week-row timeline-week-row--header"'),
            rendered.index('class="timeline-course-group '),
        )
        self.assertNotIn("患者・Course別 Gantt", rendered)
        self.assertEqual(rendered.count('style="--timeline-weeks: 12;"'), 1)
        self.assertNotIn("timeline-summary", rendered)
        self.assertNotIn("timeline-axis--weekly", rendered)
        self.assertEqual(rendered.count('class="timeline-week-cell timeline-week-cell--header'), 12)

    def test_calendar_template_explains_unknown_end_resource_and_rtms_statuses(self):
        InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="admitted",
            planned_admission_date=date(2026, 10, 1),
        )
        ResourceAssignment.objects.create(
            treatment_course=self.course_one,
            resource_pool=self.pool,
            planned_start_date=date(2026, 10, 3),
            planned_end_date=date(2026, 10, 5),
        )
        for session_date, status in (
            (date(2026, 10, 6), "planned"),
            (date(2026, 10, 7), "done"),
            (date(2026, 10, 8), "skipped"),
        ):
            TreatmentSession.objects.create(
                patient=self.patient,
                treatment_course=self.course_one,
                course_number=1,
                session_date=session_date,
                status=status,
            )

        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {
                "patient_id": self.patient.pk,
                "course_number": 1,
                "year": 2026,
                "month": 10,
            },
        )

        self.assertContains(response, 'data-discharge-end=""')
        self.assertNotContains(response, "退院予定未設定")
        self.assertContains(response, 'data-treatment-start="2026-10-06"')
        self.assertContains(response, 'data-treatment-end="2026-10-08"')
        row = next(
            row for row in response.context["weekly_calendar_rows"]
            if row["treatment_course_id"] == self.course_one.pk
        )
        self.assertIsNone(row["rtms_planned_period"])
        rendered = response.content.decode()
        visible_calendar = rendered.split('<div class="modal fade" id="reservationModal"', 1)[0]
        self.assertNotIn(self.pool.display_name, visible_calendar)
        self.assertNotIn("rTMS：予定1回 / 実施1回 / スキップ1回", visible_calendar)

    def test_calendar_includes_active_waitlist_entries(self):
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_two,
            status="waiting",
            priority=2,
        )
        response = self.client.get(reverse("rtms_app:inpatient_calendar"))
        self.assertContains(response, "待機患者")
        self.assertContains(response, "Course #2")
        self.assertContains(response, "入院手続きが完了した状態")

    def test_calendar_month_navigation_uses_current_month_and_preserves_course_context(self):
        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"patient_id": self.patient.pk, "course_number": 2},
        )

        self.assertEqual(response.status_code, 200)
        today = django_timezone.localdate()
        self.assertEqual(response.context["year"], today.year)
        self.assertEqual(response.context["month"], today.month)
        self.assertIn("patient_id={}&course_number=2".format(self.patient.pk), response.context["previous_month_url"])
        self.assertIn("patient_id={}&course_number=2".format(self.patient.pk), response.context["next_month_url"])

    def test_calendar_month_navigation_handles_requested_month_and_year_boundaries(self):
        october = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"year": 2026, "month": 10},
        )
        self.assertEqual(october.context["year"], 2026)
        self.assertEqual(october.context["month"], 10)
        self.assertIn("year=2026&month=9", october.context["previous_month_url"])
        self.assertIn("year=2026&month=11", october.context["next_month_url"])

        january = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"year": 2026, "month": 1},
        )
        self.assertIn("year=2025&month=12", january.context["previous_month_url"])

        december = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"year": 2026, "month": 12},
        )
        self.assertIn("year=2027&month=1", december.context["next_month_url"])

    def test_calendar_today_link_returns_to_timezone_local_month(self):
        response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"year": 2026, "month": 10},
        )

        today = django_timezone.localdate()
        self.assertIn(f"year={today.year}&month={today.month}", response.context["today_month_url"])

    def test_actual_discharge_overrides_planned_and_unknown_end_is_explicit(self):
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_one, status="scheduled",
            planned_admission_date=date(2026, 10, 1),
            planned_discharge_date=date(2026, 10, 10),
            actual_discharge_at=datetime(2026, 10, 8, 10, tzinfo=timezone.utc),
        )
        context = build_inpatient_calendar(year=2026, month=10, treatment_course=self.course_one)
        day_eight = next(day for day in context["days"] if day["date"] == date(2026, 10, 8))
        day_nine = next(day for day in context["days"] if day["date"] == date(2026, 10, 9))
        self.assertEqual(day_eight["inpatient_count"], 1)
        self.assertEqual(day_nine["inpatient_count"], 0)
        plan.actual_discharge_at = None
        plan.planned_discharge_date = None
        plan.estimated_discharge_date = None
        plan.save(update_fields=["actual_discharge_at", "planned_discharge_date", "estimated_discharge_date", "updated_at"])
        unknown_context = build_inpatient_calendar(year=2026, month=10, treatment_course=self.course_one)
        self.assertTrue(any(day["unknown_end_count"] for day in unknown_context["days"]))
        unknown_timeline = unknown_context["calendar_rows"][0]["timelines"]["inpatient"][0]
        self.assertIsNone(unknown_timeline["end"])
        self.assertEqual(unknown_timeline["end_kind"], "unknown")

    def test_forecast_is_provisional_and_reports_unknown_discharge(self):
        InpatientPlan.objects.create(
            treatment_course=self.course_one, status="admitted",
            planned_admission_date=date(2026, 9, 1),
        )
        forecast = earliest_admission_forecast(date(2026, 10, 1), resource_pool=self.pool)
        self.assertIn("見込み", forecast["label"])
        self.assertEqual(forecast["uncertainty_count"], 1)

    def test_initial_visit_exposes_both_phase2_paths(self):
        response = self.client.get(
            reverse("rtms_app:patient_first_visit", args=[self.patient.pk]),
            {"course_number": 1},
        )
        self.assertContains(response, reverse("rtms_app:inpatient_calendar"))
        self.assertContains(response, reverse("rtms_app:rtms_waitlist", args=[self.patient.pk]))
        self.assertContains(response, "rTMS待機者リストに登録")
        self.assertNotContains(response, "rTMS日程を相談")
        self.assertNotContains(response, "rTMS調整画面へ")
        self.assertNotContains(response, ">待機者登録</a>")

    def test_initial_visit_places_schedule_fields_in_requested_order(self):
        self.course_one.first_visit_date = date(2026, 9, 10)
        self.course_one.save(update_fields=["first_visit_date", "updated_at"])
        response = self.client.get(
            reverse("rtms_app:patient_first_visit", args=[self.patient.pk]),
            {"course_number": 1},
        )
        content = response.content.decode()
        schedule_start = content.index("スケジュール・担当医")
        schedule = content[schedule_start:]
        self.assertIn('name="first_visit_date"', schedule)
        self.assertIn('value="2026-09-10"', schedule)
        field_order = (
            "初診日",
            "入院予定日",
            "初回治療日",
            "rTMS日程調整",
            "特別個室利用予定",
            "今後の担当医",
        )
        positions = [schedule.index(label) for label in field_order]
        self.assertEqual(positions, sorted(positions))

    def test_initial_visit_shows_registered_state_per_course(self):
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="waiting",
        )
        response = self.client.get(
            reverse("rtms_app:patient_first_visit", args=[self.patient.pk]),
            {"course_number": 1},
        )
        self.assertContains(response, "待機者登録済み")
        self.assertNotContains(response, "rTMS待機者リストに登録")

        course_two_response = self.client.get(
            reverse("rtms_app:patient_first_visit", args=[self.patient.pk]),
            {"course_number": 2},
        )
        self.assertContains(course_two_response, "rTMS待機者リストに登録")

    def test_primary_calendar_navigation_links_are_available(self):
        dashboard = self.client.get(reverse("rtms_app:dashboard"))
        month = self.client.get(reverse("rtms_app:calendar_month"))
        rtms = self.client.get(reverse("rtms_app:inpatient_calendar"))
        self.assertContains(dashboard, reverse("rtms_app:inpatient_calendar"))
        self.assertContains(month, reverse("rtms_app:dashboard"))
        self.assertContains(month, reverse("rtms_app:patient_list"))
        self.assertContains(month, reverse("rtms_app:inpatient_calendar"))
        self.assertContains(rtms, reverse("rtms_app:dashboard"))
        self.assertContains(rtms, reverse("rtms_app:patient_list"))
        self.assertContains(rtms, reverse("rtms_app:calendar_month"))

    def test_initial_visit_private_room_planned_is_course_scoped(self):
        physician_group = Group.objects.create(name="医師")
        self.user.groups.add(physician_group)
        response = self.client.get(
            reverse("rtms_app:patient_first_visit", args=[self.patient.pk]),
            {"course_number": 1},
        )
        self.assertContains(response, "特別個室利用予定")
        self.client.post(
            reverse("rtms_app:patient_first_visit", args=[self.patient.pk]),
            {
                "course_number": 1,
                "first_visit_date": "2026-09-16",
                "admission_date": "2026-10-01",
                "first_treatment_date": "2026-10-02",
                "attending_physician": str(self.user.pk),
                "private_room_planned": "on",
            },
        )
        self.course_one.refresh_from_db()
        self.course_two.refresh_from_db()
        self.assertTrue(self.course_one.private_room_planned)
        self.assertFalse(self.course_two.private_room_planned)
        calendar = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"year": 2026, "month": 10},
        )
        visible_calendar = calendar.content.decode().split(
            '<div class="modal fade" id="reservationModal"', 1
        )[0]
        self.assertNotIn("退院予定未設定", visible_calendar)
        self.assertNotIn("特別個室利用予定", visible_calendar)
        self.assertNotIn("rTMS 2026/10/02", visible_calendar)

    def test_phase2_entry_paths_render_for_selected_course(self):
        calendar_response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"patient_id": self.patient.pk, "course_number": self.course_one.course_number},
        )
        waitlist_response = self.client.get(
            reverse("rtms_app:rtms_waitlist", args=[self.patient.pk]),
            {"course_number": self.course_one.course_number},
        )
        self.assertEqual(calendar_response.status_code, 200)
        self.assertEqual(waitlist_response.status_code, 200)
        self.assertContains(calendar_response, "選択中: Phase Two")
        self.assertContains(waitlist_response, "rTMS待機リスト")

    def test_calendar_reorganizes_global_view_with_waitlist_and_course_filter(self):
        InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="scheduled",
            planned_admission_date=date(2026, 10, 1),
            planned_discharge_date=date(2026, 10, 10),
        )
        InpatientPlan.objects.create(
            treatment_course=self.course_two,
            status="provisional",
            planned_admission_date=date(2026, 10, 12),
            planned_discharge_date=date(2026, 10, 20),
        )
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="waiting",
            priority=2,
            preferred_start_note="10月中旬希望",
        )
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_two,
            status="requested",
            priority=1,
            preferred_start_note="11月希望",
        )

        global_response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {"year": 2026, "month": 10},
        )
        self.assertEqual(global_response.status_code, 200)
        self.assertContains(global_response, "rTMS日程調整")
        self.assertContains(global_response, "患者ガントチャート")
        self.assertNotContains(global_response, "② 期間集計")
        self.assertNotContains(global_response, "表示対象:")
        self.assertEqual(len(global_response.context["weekly_calendar_rows"]), 2)
        self.assertEqual(global_response.context["treatment_calendar_rows"], [])
        self.assertContains(global_response, "timeline-header-row")
        self.assertNotContains(global_response, "timeline-course-group")
        self.assertNotContains(global_response, "timeline-track--schedule")
        self.assertNotContains(global_response, "timeline-summary")
        self.assertNotContains(global_response, 'timeline-lane-label">治療予約</div>')
        global_html = global_response.content.decode()
        self.assertEqual(global_html.count('class="timeline-week-row timeline-week-row--header"'), 1)
        self.assertEqual(global_html.count('class="timeline-week-cell timeline-week-cell--header'), 12)
        self.assertEqual(global_html.count('class="timeline-week-row timeline-week-row--capacity"'), 1)
        self.assertEqual(global_html.count('class="timeline-week-cell timeline-week-cell--capacity'), 12)
        self.assertEqual(
            global_html.count('class="timeline-track timeline-track--schedule"'),
            len(global_response.context["treatment_calendar_rows"]),
        )
        self.assertNotIn("TreatmentSession", global_html)
        self.assertEqual(global_html.count('schedule-status--waiting">待機</span>'), 0)
        self.assertContains(global_response, "表示期間内にrTMS治療バーはありません。")
        self.assertNotIn("schedule-status--unreserved", global_html)
        self.assertContains(global_response, "待機患者")
        self.assertNotContains(global_response, "④ 日別詳細")
        self.assertNotContains(global_response, "日別詳細を表示")
        self.assertContains(global_response, "Course #1")
        self.assertContains(global_response, "Course #2")
        self.assertContains(global_response, "待機登録")
        self.assertContains(global_response, "日程設定")
        self.assertNotContains(global_response, ">待機者登録</a>")
        self.assertContains(
            global_response,
            f"{reverse('rtms_app:patient_first_visit', args=[self.patient.pk])}?course_number=1",
        )
        self.assertContains(
            global_response,
            f"{reverse('rtms_app:patient_first_visit', args=[self.patient.pk])}?course_number=2",
        )
        self.assertContains(global_response, "data-course-id=\"{}\"".format(self.course_one.pk))
        self.assertContains(global_response, "data-course-id=\"{}\"".format(self.course_two.pk))

        filtered_response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {
                "patient_id": self.patient.pk,
                "course_number": 1,
                "year": 2026,
                "month": 10,
            },
        )
        self.assertContains(filtered_response, self.patient.name)
        self.assertNotContains(filtered_response, "選択中:")
        self.assertContains(filtered_response, "Course #1")
        self.assertContains(filtered_response, "10月中旬希望")
        self.assertNotContains(filtered_response, "2026/11/01")

    def test_weekly_private_room_planned_fallback_and_assignment_priority(self):
        self.course_one.private_room_planned = True
        self.course_one.save(update_fields=["private_room_planned", "updated_at"])
        InpatientPlan.objects.create(
            treatment_course=self.course_one,
            status="scheduled",
            planned_admission_date=date(2026, 9, 18),
            planned_discharge_date=date(2026, 10, 2),
        )
        planned_context = build_inpatient_calendar_range(
            start_date=date(2026, 9, 16),
            today=date(2026, 9, 16),
            treatment_course=self.course_one,
        )
        planned_item = planned_context["weekly_calendar_rows"][0]["timelines"]["private_room"][0]
        self.assertTrue(planned_item["planned_only"])
        self.assertIsNone(planned_item["resource_pool"])
        self.assertEqual(planned_context["weekly_summary"]["peak_private_rooms"], 1)

        assignment = ResourceAssignment.objects.create(
            treatment_course=self.course_one,
            resource_pool=self.pool,
            planned_start_date=date(2026, 9, 28),
            planned_end_date=date(2026, 10, 2),
        )
        assigned_context = build_inpatient_calendar_range(
            start_date=date(2026, 9, 16),
            today=date(2026, 9, 16),
            treatment_course=self.course_one,
        )
        assigned_item = assigned_context["weekly_calendar_rows"][0]["timelines"]["private_room"][0]
        self.assertFalse(assigned_item["planned_only"])
        self.assertEqual(assigned_item["assignment"].pk, assignment.pk)
        self.assertEqual(assigned_item["start"], assignment.planned_start_date)

    def test_waitlist_post_redirects_with_patient_and_course_and_keeps_isolation(self):
        response = self.client.post(
            reverse("rtms_app:rtms_waitlist", args=[self.patient.pk])
            + "?course_number=1",
            {
                "preferred_start_note": "2026/10/15〜10/20希望、入院14日程度",
                "priority": "2",
                "comment": "希望日程あり",
                "course_number": "1",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            response["Location"],
            reverse("rtms_app:rtms_waitlist", args=[self.patient.pk])
            + "?course_number=1",
        )
        entry = self.course_one.rtms_waitlist_entries.get()
        self.assertEqual(entry.status, "waiting")
        self.assertEqual(entry.preferred_start_note, "2026/10/15〜10/20希望、入院14日程度")
        self.assertEqual(entry.comment, "希望日程あり")
        self.assertEqual(ResourceAssignment.objects.count(), 0)

        course_two_response = self.client.get(
            reverse("rtms_app:rtms_waitlist", args=[self.patient.pk])
            + "?course_number=2",
        )
        self.assertEqual(course_two_response.status_code, 200)
        self.assertNotContains(course_two_response, "希望日程あり")

    def test_waitlist_registration_ui_omits_priority(self):
        response = self.client.get(
            reverse("rtms_app:rtms_waitlist", args=[self.patient.pk]),
            {"course_number": 1},
        )
        self.assertNotContains(response, "priority")
        self.assertNotContains(response, "優先度")
        self.assertContains(response, "希望: 10月中旬希望")
        self.assertNotContains(response, "希望開始時期（早い方）")
        self.assertNotContains(response, "希望開始時期（遅い方）")
        self.assertNotContains(response, "希望入院日数")

    def test_waitlist_registration_stores_editable_planned_treatment_count(self):
        response = self.client.post(
            reverse("rtms_app:rtms_waitlist", args=[self.patient.pk]) + "?course_number=1",
            {"planned_treatment_sessions": "24", "course_number": "1"},
        )
        self.assertEqual(response.status_code, 302)
        self.course_one.refresh_from_db()
        self.assertEqual(self.course_one.planned_treatment_sessions, 24)
        self.assertNotContains(
            self.client.get(
                reverse("rtms_app:rtms_waitlist", args=[self.patient.pk]),
                {"course_number": 1},
            ),
            "希望治療期間",
        )

    def test_planned_treatment_count_defaults_to_30_per_course(self):
        self.assertEqual(self.course_one.planned_treatment_sessions, 30)
        self.assertEqual(self.course_two.planned_treatment_sessions, 30)

    def test_waitlist_schedule_expansion_is_compact_and_not_always_open(self):
        entry = RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="waiting",
            preferred_start_note="10月中旬希望",
        )
        response = self.client.get(reverse("rtms_app:inpatient_calendar"))
        self.assertContains(response, "日程設定")
        self.assertContains(response, "希望: 10月中旬希望")
        self.assertNotContains(response, "希望開始日")
        self.assertNotContains(response, "希望終了日")
        self.assertNotContains(response, "希望入院日数")
        self.assertContains(response, "booking-date-grid")
        self.assertContains(response, "booking-flags")
        self.assertNotContains(response, ">待機者登録</a>")
        self.assertContains(response, f'data-waitlist-id="{entry.pk}"')

    def test_waitlist_schedule_saves_preferences_course_fields_and_no_sessions(self):
        entry_one = RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="waiting",
            preferred_start_note="10月希望",
        )
        entry_two = RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_two,
            status="waiting",
            preferred_start_note="11月希望",
        )
        response = self.client.post(
            reverse("rtms_app:inpatient_calendar"),
            {
                "action": "save_calendar_course",
                "treatment_course_id": self.course_one.pk,
                "waitlist_entry_id": entry_one.pk,
                "preferred_start_note": "2026/10/15〜10/20希望",
                "admission_date": "2026-10-05",
                "first_treatment_date": "2026-10-08",
                "private_room_planned": "on",
                "is_all_case_survey": "on",
                "planned_treatment_sessions": "28",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.course_one.refresh_from_db()
        self.course_two.refresh_from_db()
        entry_one.refresh_from_db()
        entry_two.refresh_from_db()
        self.assertEqual(entry_one.preferred_start_note, "2026/10/15〜10/20希望")
        self.assertEqual(self.course_one.admission_date, date(2026, 10, 5))
        self.assertEqual(self.course_one.first_treatment_date, date(2026, 10, 8))
        self.assertFalse(self.course_one.private_room_planned)
        self.assertTrue(self.course_one.is_all_case_survey)
        self.assertEqual(self.course_one.planned_treatment_sessions, 30)
        self.assertIsNone(self.course_two.admission_date)
        self.assertEqual(entry_two.preferred_start_note, "11月希望")
        self.assertEqual(TreatmentSession.objects.count(), 0)

    def test_course_without_treatment_start_has_no_virtual_assessment_events(self):
        calendar_weeks, assessment_events = generate_calendar_weeks(
            self.patient, treatment_course=self.course_one,
        )
        self.assertTrue(calendar_weeks)
        self.assertEqual(assessment_events, [])
        self.assertEqual(Assessment.objects.count(), 0)
        self.assertEqual(AssessmentRecord.objects.count(), 0)

    def test_management_waitlist_renders_list(self):
        response = self.client.get(reverse("rtms_app:inpatient_waitlist"))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "rtms_app/rtms_waitlist_management.html")

    def test_management_waitlist_filters_orders_and_preserves_course_links(self):
        registered_at = datetime.now(timezone.utc) - timedelta(days=6)
        later_entry = RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="waiting",
            priority=9,
            registered_at=registered_at + timedelta(days=1),
        )
        earlier_entry = RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_two,
            status="requested",
            priority=0,
            registered_at=registered_at,
        )
        course_three = TreatmentCourse.objects.create(
            patient=self.patient, course_number=3,
        )
        provisional_entry = RtmSWaitlistEntry.objects.create(
            treatment_course=course_three,
            status="provisional",
            registered_at=registered_at + timedelta(days=2),
        )
        scheduled_course = TreatmentCourse.objects.create(
            patient=self.patient, course_number=4,
        )
        RtmSWaitlistEntry.objects.create(
            treatment_course=scheduled_course,
            status="scheduled",
            priority=9,
        )
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_two,
            status="cancelled",
        )
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_two,
            status="withdrawn",
        )

        response = self.client.get(reverse("rtms_app:inpatient_waitlist"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [entry.pk for entry in response.context["entries"]],
            [earlier_entry.pk, later_entry.pk],
        )
        self.assertContains(response, "Course #1")
        self.assertContains(response, "Course #2")
        self.assertNotContains(response, "Course #3")
        self.assertNotContains(response, "Course #4")
        self.assertNotContains(response, "キャンセル")
        self.assertNotContains(response, "撤回")
        for course_number in (1, 2):
            self.assertContains(
                response,
                f'href="{reverse("rtms_app:patient_first_visit", args=[self.patient.pk])}?course_number={course_number}"',
            )
        self.assertNotContains(response, "waiting_days")
        self.assertNotContains(response, "status_filter_choices")
        self.assertEqual(ResourceAssignment.objects.count(), 0)
        self.assertEqual(InpatientPlan.objects.count(), 0)

    def test_management_waitlist_empty_state_excludes_scheduled_entries(self):
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="scheduled",
        )

        response = self.client.get(reverse("rtms_app:inpatient_waitlist"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["entries"], [])
        self.assertContains(response, "現在、rTMS待機者はいません。")

    def test_calendar_waitlist_rows_preserve_each_course(self):
        self.course_one.admission_date = date(2026, 10, 1)
        self.course_two.admission_date = date(2026, 10, 8)
        self.course_one.save(update_fields=["admission_date", "updated_at"])
        self.course_two.save(update_fields=["admission_date", "updated_at"])
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_one,
            status="waiting",
        )
        RtmSWaitlistEntry.objects.create(
            treatment_course=self.course_two,
            status="waiting",
        )

        response = self.client.get(reverse("rtms_app:inpatient_calendar"))

        self.assertContains(
            response,
            f"{reverse('rtms_app:patient_first_visit', args=[self.patient.pk])}?course_number=1",
        )
        self.assertContains(
            response,
            f"{reverse('rtms_app:patient_first_visit', args=[self.patient.pk])}?course_number=2",
        )

    def test_waitlist_registration_patient_selection_searches_id_and_name(self):
        response = self.client.get(
            reverse("rtms_app:rtms_waitlist_patient_select"),
            {"card": self.patient.card_id},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Course #1")
        self.assertContains(response, "Course #2")

        name_response = self.client.get(
            reverse("rtms_app:rtms_waitlist_patient_select"),
            {"q": "Phase Two"},
        )
        self.assertContains(name_response, "Course #1")
        self.assertContains(name_response, "Course #2")

    def test_waitlist_registration_patient_selection_preserves_course(self):
        response = self.client.get(reverse("rtms_app:rtms_waitlist_patient_select"))
        self.assertContains(
            response,
            f'href="{reverse("rtms_app:patient_first_visit", args=[self.patient.pk])}?course_number=1">{self.patient.name}</a>',
        )
        self.assertContains(
            response,
            f'href="{reverse("rtms_app:patient_first_visit", args=[self.patient.pk])}?course_number=2">{self.patient.name}</a>',
        )
        self.assertNotContains(response, "初診画面へ")

    def test_calendar_navigation_preserves_course_from_waitlist_destination(self):
        calendar_response = self.client.get(
            reverse("rtms_app:inpatient_calendar"),
            {
                "patient_id": self.patient.pk,
                "course_number": self.course_two.course_number,
                "year": 2026,
                "month": 10,
            },
        )

        self.assertEqual(calendar_response.context["treatment_course"], self.course_two)
        for link_name in ("previous_month_url", "next_month_url", "today_month_url"):
            self.assertIn(f"patient_id={self.patient.pk}", calendar_response.context[link_name])
            self.assertIn("course_number=2", calendar_response.context[link_name])


class InpatientConfirmationRetryContractTests(TransactionTestCase):
    reset_sequences = True
    admission_date = date(2026, 10, 5)

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="confirmation-retry-contract", password="pw",
        )
        self.patient = Patient.objects.create(
            card_id="CONFIRMRETRY",
            name="Confirmation Retry",
            birth_date=date(1980, 1, 1),
        )
        self.course_one = TreatmentCourse.objects.create(
            patient=self.patient, course_number=1,
        )
        self.course_two = TreatmentCourse.objects.create(
            patient=self.patient, course_number=2,
        )
        self.pool = ResourcePool.objects.create(
            code="CONFIRM_RETRY_POOL",
            name="Confirmation Retry Pool",
            resource_type="rTMS_private",
            physical_capacity=5,
            operational_target=3,
        )
        RtmSAdmissionCapacity.objects.create(
            capacity=3,
            valid_from=date(2026, 10, 1),
            valid_to=date(2026, 10, 31),
        )

    def _confirm(self, treatment_course):
        return confirm_inpatient_schedule(
            treatment_course=treatment_course,
            resource_pool=self.pool,
            admission_date=self.admission_date,
            treatment_start_date=self.admission_date + timedelta(days=1),
            room_start_date=self.admission_date,
            room_end_date=self.admission_date + timedelta(days=10),
            user=self.user,
        )

    def _raise_busy_reservation_error(self):
        raise TreatmentReservationBusyError("reservation busy") from _SQLiteBusyCause()

    def test_mutex_precedes_course_and_capacity_locks_and_course_two_stays_isolated(self):
        with patch(
            "rtms_app.services.inpatient_planning.create_or_replace_rtms_treatment_reservation",
            return_value=None,
        ), CaptureQueriesContext(connection) as captured:
            self._confirm(self.course_two)

        statements = [query["sql"].upper() for query in captured.captured_queries]
        patient_mutex = next(
            index for index, sql in enumerate(statements)
            if sql.lstrip().startswith("UPDATE") and "RTMS_APP_PATIENT" in sql
        )
        course_lock = next(
            index for index, sql in enumerate(statements)
            if sql.lstrip().startswith("SELECT") and "RTMS_APP_TREATMENTCOURSE" in sql
        )
        capacity_lock = next(
            index for index, sql in enumerate(statements)
            if sql.lstrip().startswith("UPDATE") and "RTMS_APP_RTMSADMISSIONCAPACITY" in sql
        )
        self.assertLess(patient_mutex, course_lock)
        self.assertLess(course_lock, capacity_lock)
        self.assertFalse(InpatientPlan.objects.filter(treatment_course=self.course_one).exists())
        self.assertTrue(InpatientPlan.objects.filter(treatment_course=self.course_two).exists())
        self.course_one.refresh_from_db()
        self.course_two.refresh_from_db()
        self.assertIsNone(self.course_one.admission_date)
        self.assertEqual(self.course_two.admission_date, self.admission_date)

    def test_reservation_busy_rolls_back_before_retrying_whole_confirmation(self):
        attempts = []

        def fail_once_then_continue(**kwargs):
            attempts.append(None)
            if len(attempts) == 1:
                self._raise_busy_reservation_error()
            return None

        def assert_first_attempt_was_rolled_back(retry_number):
            self.assertEqual(retry_number, 1)
            self.assertFalse(InpatientPlan.objects.filter(treatment_course=self.course_one).exists())
            self.assertFalse(ResourceAssignment.objects.filter(treatment_course=self.course_one).exists())
            self.course_one.refresh_from_db()
            self.assertIsNone(self.course_one.admission_date)

        with patch(
            "rtms_app.services.inpatient_planning.create_or_replace_rtms_treatment_reservation",
            side_effect=fail_once_then_continue,
        ), patch(
            "rtms_app.services.inpatient_planning._sleep_before_confirmation_retry",
            side_effect=assert_first_attempt_was_rolled_back,
        ) as retry_sleep:
            result = self._confirm(self.course_one)

        self.assertEqual(len(attempts), 2)
        retry_sleep.assert_called_once_with(1)
        self.assertEqual(result["plan"].status, "scheduled")
        self.assertTrue(InpatientPlan.objects.filter(treatment_course=self.course_one).exists())

    def test_retry_exhaustion_is_a_controlled_view_error_without_partial_writes(self):
        self.client.force_login(self.user)
        with patch(
            "rtms_app.services.inpatient_planning.create_or_replace_rtms_treatment_reservation",
            side_effect=lambda **kwargs: self._raise_busy_reservation_error(),
        ), patch(
            "rtms_app.services.inpatient_planning._sleep_before_confirmation_retry",
        ) as retry_sleep:
            response = self.client.post(
                reverse("rtms_app:inpatient_schedule", args=[self.patient.pk]),
                {
                    "course_number": "1",
                    "admission_date": "2026-10-05",
                    "treatment_start_date": "2026-10-06",
                    "room_start_date": "2026-10-05",
                    "room_end_date": "2026-10-15",
                    "certainty": "confirmed",
                    "resource_pool": str(self.pool.pk),
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "同時処理のため確定処理を完了できませんでした。もう一度お試しください。",
            list(response.context["form"].non_field_errors()),
        )
        self.assertEqual(retry_sleep.call_count, 2)
        self.assertFalse(InpatientPlan.objects.filter(treatment_course=self.course_one).exists())
        self.assertFalse(ResourceAssignment.objects.filter(treatment_course=self.course_one).exists())
        self.course_one.refresh_from_db()
        self.assertIsNone(self.course_one.admission_date)


class AdmissionCapacityConcurrencyTests(TransactionTestCase):
    reset_sequences = True
    admission_date = date(2026, 10, 5)

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if connections["default"].vendor != "sqlite":
            raise RuntimeError("Admission capacity concurrency tests require SQLite")

        database = connections["default"]
        database.ensure_connection()
        cls._original_database_name = database.settings_dict["NAME"]
        cls._test_database_directory = tempfile.TemporaryDirectory(prefix="rtms-capacity-test-")
        cls._file_database_name = os.path.join(cls._test_database_directory.name, "test.sqlite3")
        with sqlite3.connect(cls._file_database_name) as file_database:
            database.connection.backup(file_database)

        database.close()
        connections.databases["default"]["NAME"] = cls._file_database_name
        database.settings_dict["NAME"] = cls._file_database_name

    @classmethod
    def tearDownClass(cls):
        try:
            connections.close_all()
            connections.databases["default"]["NAME"] = cls._original_database_name
            connections["default"].settings_dict["NAME"] = cls._original_database_name
            cls._test_database_directory.cleanup()
        finally:
            super().tearDownClass()

    def setUp(self):
        self.user = get_user_model().objects.create_user(username="capacity-concurrency", password="pw")
        self.capacity = RtmSAdmissionCapacity.objects.create(
            capacity=3,
            valid_from=date(2026, 10, 1),
            valid_to=date(2026, 10, 31),
        )
        self.courses = []
        self.pools = []
        for index in range(3):
            patient = Patient.objects.create(
                card_id=f"8300{index + 1}",
                name=f"Concurrency {index + 1}",
                birth_date=date(1980, 1, 1),
            )
            self.courses.append(TreatmentCourse.objects.create(patient=patient, course_number=1))
            self.pools.append(ResourcePool.objects.create(
                code=f"CAPACITY_POOL_{index + 1}",
                name=f"Capacity pool {index + 1}",
                resource_type="rTMS_private",
                physical_capacity=5,
                operational_target=3,
            ))

    @staticmethod
    def _configure_thread_connection():
        connections.close_all()
        database = connections["default"]
        options = dict(database.settings_dict.get("OPTIONS", {}))
        options["timeout"] = 0.05
        database.settings_dict["OPTIONS"] = options

    def _confirm_in_thread(self, course_id, pool_id, barrier=None):
        self._configure_thread_connection()
        try:
            course = TreatmentCourse.objects.get(pk=course_id)
            pool = ResourcePool.objects.get(pk=pool_id)
            if barrier is not None:
                barrier.wait(timeout=5)
            confirm_inpatient_schedule(
                treatment_course=course,
                resource_pool=pool,
                admission_date=self.admission_date,
                treatment_start_date=self.admission_date + timedelta(days=1),
                room_start_date=self.admission_date,
                room_end_date=self.admission_date + timedelta(days=10),
                user=self.user,
            )
            return "confirmed"
        except CapacityConflict:
            return "capacity"
        finally:
            connections.close_all()

    def _run_concurrent_confirms(self, course_indexes):
        barrier = Barrier(len(course_indexes))
        with ThreadPoolExecutor(max_workers=len(course_indexes)) as executor:
            futures = [
                executor.submit(
                    self._confirm_in_thread,
                    self.courses[index].pk,
                    self.pools[index].pk,
                    barrier,
                )
                for index in course_indexes
            ]
            return [future.result(timeout=15) for future in futures]

    def _start_capacity_writer(self, acquired, release, finished):
        def hold_writer():
            self._configure_thread_connection()
            try:
                with transaction.atomic():
                    RtmSAdmissionCapacity.objects.filter(pk=self.capacity.pk).update(
                        capacity=F("capacity"),
                    )
                    acquired.set()
                    if not release.wait(timeout=5):
                        raise TimeoutError("test writer was not released")
            finally:
                connections.close_all()
                finished.set()

        thread = Thread(target=hold_writer, daemon=True)
        thread.start()
        self.assertTrue(acquired.wait(timeout=5), "test writer did not acquire SQLite write access")
        return thread

    def _start_course_reader(self, course_id, acquired, release, finished):
        def hold_reader():
            self._configure_thread_connection()
            try:
                with transaction.atomic():
                    TreatmentCourse.objects.get(pk=course_id)
                    acquired.set()
                    if not release.wait(timeout=5):
                        raise TimeoutError("test reader was not released")
            finally:
                connections.close_all()
                finished.set()

        thread = Thread(target=hold_reader, daemon=True)
        thread.start()
        self.assertTrue(acquired.wait(timeout=5), "test reader did not open its transaction")
        return thread

    def _release_on_retry(self, release, finished):
        def release_after_busy(_retry_number):
            release.set()
            if not finished.wait(timeout=5):
                raise TimeoutError("test writer did not finish after retry was triggered")

        return release_after_busy

    def test_capacity_one_allows_at_most_one_concurrent_confirm_across_pools(self):
        self.capacity.capacity = 1
        self.capacity.save(update_fields=["capacity", "updated_at"])

        results = self._run_concurrent_confirms((0, 1))

        self.assertLessEqual(results.count("confirmed"), 1)
        self.assertLessEqual(
            InpatientPlan.objects.filter(status__in=("draft", "provisional", "scheduled", "admitted")).count(),
            1,
        )
        for course in self.courses[:2]:
            self.assertLessEqual(ResourceAssignment.objects.filter(treatment_course=course).count(), 1)

    def test_capacity_two_allows_at_most_two_of_three_concurrent_confirms(self):
        self.capacity.capacity = 2
        self.capacity.save(update_fields=["capacity", "updated_at"])

        results = self._run_concurrent_confirms((0, 1, 2))

        self.assertLessEqual(results.count("confirmed"), 2)
        self.assertLessEqual(
            InpatientPlan.objects.filter(status__in=("draft", "provisional", "scheduled", "admitted")).count(),
            2,
        )
        for course in self.courses:
            self.assertLessEqual(ResourceAssignment.objects.filter(treatment_course=course).count(), 1)

    def test_sqlite_busy_retries_after_writer_releases_and_commits_once(self):
        acquired, release, finished = Event(), Event(), Event()
        writer = self._start_capacity_writer(acquired, release, finished)

        with patch(
            "rtms_app.services.inpatient_planning._sleep_before_confirmation_retry",
            side_effect=self._release_on_retry(release, finished),
        ) as retry_sleep:
            with ThreadPoolExecutor(max_workers=1) as executor:
                result = executor.submit(
                    self._confirm_in_thread,
                    self.courses[0].pk,
                    self.pools[0].pk,
                ).result(timeout=10)

        writer.join(timeout=5)
        self.assertFalse(writer.is_alive())
        self.assertEqual(result, "confirmed")
        retry_sleep.assert_called_once()
        self.assertEqual(InpatientPlan.objects.filter(treatment_course=self.courses[0]).count(), 1)
        self.assertEqual(ResourceAssignment.objects.filter(treatment_course=self.courses[0]).count(), 1)

    def test_retry_exhaustion_is_rendered_as_a_bound_form_error(self):
        self.client.force_login(self.user)
        acquired, release, finished = Event(), Event(), Event()
        writer = self._start_capacity_writer(acquired, release, finished)
        retry_numbers = []

        def confirm_on_independent_connection(**kwargs):
            def run_confirm():
                self._configure_thread_connection()
                try:
                    return confirm_inpatient_schedule(**kwargs)
                finally:
                    connections.close_all()

            with ThreadPoolExecutor(max_workers=1) as executor:
                return executor.submit(run_confirm).result(timeout=10)

        try:
            with patch(
                "rtms_app.services.inpatient_planning._sleep_before_confirmation_retry",
                side_effect=retry_numbers.append,
            ), patch(
                "rtms_app.views_inpatient.confirm_inpatient_schedule",
                side_effect=confirm_on_independent_connection,
            ):
                response = self.client.post(
                    reverse("rtms_app:inpatient_schedule", args=[self.courses[0].patient_id]),
                    {
                        "course_number": "1",
                        "admission_date": "2026-10-05",
                        "treatment_start_date": "2026-10-06",
                        "room_start_date": "2026-10-05",
                        "room_end_date": "2026-10-15",
                        "certainty": "confirmed",
                        "resource_pool": str(self.pools[0].pk),
                    },
                )
        finally:
            release.set()
            writer.join(timeout=5)

        self.assertFalse(writer.is_alive())
        self.assertEqual(retry_numbers, [1, 2])
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertTrue(form.is_bound)
        errors = list(form.non_field_errors())
        self.assertIn(
            "同時処理のため確定処理を完了できませんでした。もう一度お試しください。",
            errors,
        )
        self.assertNotIn("rTMS新規入院受入枠を超える", " ".join(errors))
        self.assertContains(response, "同時処理のため確定処理を完了できませんでした。")
        self.assertFalse(InpatientPlan.objects.filter(treatment_course=self.courses[0]).exists())
        self.assertFalse(ResourceAssignment.objects.filter(treatment_course=self.courses[0]).exists())

    def test_commit_busy_rolls_back_first_attempt_before_successful_retry(self):
        database = connections["default"]
        database.close()
        with sqlite3.connect(self._file_database_name) as file_database:
            journal_mode = file_database.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        self.assertEqual(journal_mode.lower(), "delete")

        course = TreatmentCourse.objects.get(pk=self.courses[0].pk)
        original_admission_date = date(2026, 9, 20)
        original_treatment_date = date(2026, 9, 21)
        course.admission_date = original_admission_date
        course.first_treatment_date = original_treatment_date
        course.save(update_fields=["admission_date", "first_treatment_date", "updated_at"])

        reader_acquired, release_reader, reader_finished = Event(), Event(), Event()
        reader = self._start_course_reader(
            course.pk,
            reader_acquired,
            release_reader,
            reader_finished,
        )
        executed_sql = []

        def record_sql(execute, sql, params, many, context):
            executed_sql.append(sql)
            return execute(sql, params, many, context)

        def assert_rolled_back_then_release(retry_number):
            try:
                self.assertEqual(retry_number, 1)
                sql_writes = [statement.lower() for statement in executed_sql]
                for table in (
                    "rtms_app_inpatientplan",
                    "rtms_app_resourceassignment",
                    "rtms_app_treatmentcourse",
                ):
                    self.assertTrue(
                        any(table in statement and statement.lstrip().startswith(("insert", "update"))
                            for statement in sql_writes),
                        f"first attempt did not write {table}",
                    )

                rolled_back_course = TreatmentCourse.objects.get(pk=course.pk)
                self.assertEqual(rolled_back_course.admission_date, original_admission_date)
                self.assertEqual(rolled_back_course.first_treatment_date, original_treatment_date)
                self.assertFalse(InpatientPlan.objects.filter(treatment_course=course).exists())
                self.assertFalse(ResourceAssignment.objects.filter(treatment_course=course).exists())
            finally:
                release_reader.set()
                if not reader_finished.wait(timeout=5):
                    raise TimeoutError("test reader did not finish after first-attempt rollback")

        def confirm_with_sql_recording():
            self._configure_thread_connection()
            try:
                attempt_course = TreatmentCourse.objects.get(pk=course.pk)
                attempt_pool = ResourcePool.objects.get(pk=self.pools[0].pk)
                with connections["default"].execute_wrapper(record_sql):
                    return confirm_inpatient_schedule(
                        treatment_course=attempt_course,
                        resource_pool=attempt_pool,
                        admission_date=self.admission_date,
                        treatment_start_date=self.admission_date + timedelta(days=1),
                        room_start_date=self.admission_date,
                        room_end_date=self.admission_date + timedelta(days=10),
                        user=self.user,
                    )
            finally:
                connections.close_all()

        try:
            with patch(
                "rtms_app.services.inpatient_planning._sleep_before_confirmation_retry",
                side_effect=assert_rolled_back_then_release,
            ) as retry_sleep:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    result = executor.submit(confirm_with_sql_recording).result(timeout=15)
        finally:
            release_reader.set()
            reader.join(timeout=5)

        self.assertFalse(reader.is_alive())
        retry_sleep.assert_called_once_with(1)
        self.assertEqual(result["plan"].status, "scheduled")
        final_course = TreatmentCourse.objects.get(pk=course.pk)
        self.assertEqual(final_course.admission_date, self.admission_date)
        self.assertEqual(final_course.first_treatment_date, self.admission_date + timedelta(days=1))
        self.assertEqual(InpatientPlan.objects.filter(treatment_course=course).count(), 1)
        self.assertEqual(ResourceAssignment.objects.filter(treatment_course=course).count(), 1)
        self.assertEqual(
            sum("rtms_app_inpatientplan" in sql.lower() and sql.lstrip().lower().startswith("insert")
                for sql in executed_sql),
            2,
        )
        self.assertEqual(
            sum("rtms_app_resourceassignment" in sql.lower() and sql.lstrip().lower().startswith("insert")
                for sql in executed_sql),
            2,
        )

    def test_retry_reloads_count_and_rejects_second_course_after_capacity_one(self):
        self.capacity.capacity = 1
        self.capacity.save(update_fields=["capacity", "updated_at"])
        confirm_inpatient_schedule(
            treatment_course=self.courses[0],
            resource_pool=self.pools[0],
            admission_date=self.admission_date,
            treatment_start_date=self.admission_date + timedelta(days=1),
            room_start_date=self.admission_date,
            room_end_date=self.admission_date + timedelta(days=10),
            user=self.user,
        )
        acquired, release, finished = Event(), Event(), Event()
        writer = self._start_capacity_writer(acquired, release, finished)

        with patch(
            "rtms_app.services.inpatient_planning._sleep_before_confirmation_retry",
            side_effect=self._release_on_retry(release, finished),
        ) as retry_sleep:
            with ThreadPoolExecutor(max_workers=1) as executor:
                result = executor.submit(
                    self._confirm_in_thread,
                    self.courses[1].pk,
                    self.pools[1].pk,
                ).result(timeout=10)

        writer.join(timeout=5)
        self.assertFalse(writer.is_alive())
        self.assertEqual(result, "capacity")
        retry_sleep.assert_called_once()
        self.assertFalse(InpatientPlan.objects.filter(treatment_course=self.courses[1]).exists())
        self.assertFalse(ResourceAssignment.objects.filter(treatment_course=self.courses[1]).exists())

    def test_capacity_zero_rejects_without_partial_course_plan_or_assignment_write(self):
        self.capacity.capacity = 0
        self.capacity.save(update_fields=["capacity", "updated_at"])
        original_course = TreatmentCourse.objects.get(pk=self.courses[0].pk)

        with self.assertRaises(CapacityConflict):
            confirm_inpatient_schedule(
                treatment_course=original_course,
                resource_pool=self.pools[0],
                admission_date=self.admission_date,
                treatment_start_date=self.admission_date + timedelta(days=1),
                room_start_date=self.admission_date,
                room_end_date=self.admission_date + timedelta(days=10),
                user=self.user,
            )

        original_course.refresh_from_db()
        self.assertIsNone(original_course.admission_date)
        self.assertIsNone(original_course.first_treatment_date)
        self.assertFalse(InpatientPlan.objects.filter(treatment_course=original_course).exists())
        self.assertFalse(ResourceAssignment.objects.filter(treatment_course=original_course).exists())

    def test_missing_capacity_rejects_without_partial_course_plan_or_assignment_write(self):
        self.capacity.delete()
        original_course = TreatmentCourse.objects.get(pk=self.courses[0].pk)

        with self.assertRaises(CapacityConflict):
            confirm_inpatient_schedule(
                treatment_course=original_course,
                resource_pool=self.pools[0],
                admission_date=self.admission_date,
                treatment_start_date=self.admission_date + timedelta(days=1),
                room_start_date=self.admission_date,
                room_end_date=self.admission_date + timedelta(days=10),
                user=self.user,
            )

        original_course.refresh_from_db()
        self.assertIsNone(original_course.admission_date)
        self.assertIsNone(original_course.first_treatment_date)
        self.assertFalse(InpatientPlan.objects.filter(treatment_course=original_course).exists())
        self.assertFalse(ResourceAssignment.objects.filter(treatment_course=original_course).exists())

    def test_active_assignment_guard_still_rejects_same_course_and_pool(self):
        assignment = ResourceAssignment.objects.create(
            treatment_course=self.courses[0],
            resource_pool=self.pools[0],
            planned_start_date=self.admission_date,
            status="active",
            certainty="confirmed",
        )

        with self.assertRaises(ActiveResourceAssignmentError):
            confirm_inpatient_schedule(
                treatment_course=self.courses[0],
                resource_pool=self.pools[0],
                admission_date=self.admission_date,
                treatment_start_date=self.admission_date + timedelta(days=1),
                room_start_date=self.admission_date,
                room_end_date=self.admission_date + timedelta(days=10),
                user=self.user,
            )

        assignment.refresh_from_db()
        self.assertEqual(assignment.status, "active")
        self.assertFalse(InpatientPlan.objects.filter(treatment_course=self.courses[0]).exists())
        self.assertEqual(ResourceAssignment.objects.filter(treatment_course=self.courses[0]).count(), 1)
