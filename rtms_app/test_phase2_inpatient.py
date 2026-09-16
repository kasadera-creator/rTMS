from datetime import date, datetime, timezone

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from rtms_app.models import (
    InpatientPlan,
    Patient,
    ResourceAssignment,
    ResourcePool,
    RtmSAdmissionCapacity,
    TreatmentCourse,
    TreatmentSession,
)
from rtms_app.services.inpatient_planning import confirm_inpatient_schedule
from rtms_app.services.resource_capacity import (
    CapacityConflict,
    capacity_snapshot,
    earliest_admission_forecast,
)
from rtms_app.services.rtms_inpatient_calendar import build_inpatient_calendar
from rtms_app.services.waitlist import register_waitlist_entry


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
        self.course_one.refresh_from_db()
        self.assertEqual(self.course_one.first_treatment_date, date(2026, 10, 6))

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

    def test_waitlist_registration_creates_no_assignment_and_keeps_episodes(self):
        first = register_waitlist_entry(
            treatment_course=self.course_one, user=self.user,
            status="waiting", preferred_start_from=date(2026, 10, 15), priority=1,
        )
        second = register_waitlist_entry(
            treatment_course=self.course_one, user=self.user,
            status="waiting", preferred_start_to=date(2026, 11, 1), priority=2,
        )
        self.assertEqual(self.course_one.rtms_waitlist_entries.count(), 2)
        self.assertIsNone(first.inpatient_plan_id)
        self.assertIsNone(second.inpatient_plan_id)
        self.assertEqual(ResourceAssignment.objects.count(), 0)

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
