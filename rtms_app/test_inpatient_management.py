from datetime import date, datetime, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import TestCase

from rtms_app.models import (
    InpatientPlan,
    Patient,
    ResourceAssignment,
    ResourcePool,
    RtmSWaitlistEntry,
    TreatmentCourse,
)
from rtms_app.services.inpatient_planning import get_or_create_inpatient_plan
from rtms_app.services.resource_capacity import (
    capacity_snapshot,
    create_planned_assignment,
    resolve_inpatient_end_date,
)
from rtms_app.services.waitlist import register_waitlist_entry


class Phase1ModelTests(TestCase):
    def setUp(self):
        self.patient = Patient.objects.create(
            card_id="81001",
            name="Phase One",
            birth_date=date(1980, 1, 1),
        )
        self.course_one = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=1,
            discharge_date=date(2026, 12, 31),
        )
        self.course_two = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=2,
        )
        self.pool = ResourcePool.objects.create(
            code="R1_PRIVATE",
            name="rTMS個室",
            resource_type="rTMS_private",
            physical_capacity=5,
            operational_target=3,
        )
        self.user = get_user_model().objects.create_user(username="phase1-user")

    def test_course_has_one_inpatient_plan_and_course_isolation(self):
        plan = InpatientPlan.objects.create(treatment_course=self.course_one)
        self.assertEqual(self.course_one.inpatient_plan, plan)
        self.assertIsNone(InpatientPlan.objects.filter(treatment_course=self.course_two).first())
        with self.assertRaises(IntegrityError):
            InpatientPlan.objects.create(treatment_course=self.course_one)

    def test_waitlist_is_one_to_many_and_needs_no_assignment(self):
        first = register_waitlist_entry(
            treatment_course=self.course_one,
            user=self.user,
            status="waiting",
            priority=1,
        )
        second = register_waitlist_entry(
            treatment_course=self.course_one,
            user=self.user,
            status="withdrawn",
            priority=2,
        )
        self.assertEqual(self.course_one.rtms_waitlist_entries.count(), 2)
        self.assertIsNone(first.inpatient_plan_id)
        self.assertIsNone(second.inpatient_plan_id)
        self.assertEqual(ResourceAssignment.objects.count(), 0)

    def test_resource_pool_code_is_unique_and_capacity_is_configurable(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                ResourcePool.objects.create(
                    code=self.pool.code,
                    name="duplicate",
                    resource_type="rTMS_private",
                    physical_capacity=8,
                    operational_target=3,
                )
        self.pool.physical_capacity = 8
        self.pool.save(update_fields=["physical_capacity", "updated_at"])
        self.assertEqual(self.pool.refresh_from_db(), None)
        self.assertEqual(self.pool.physical_capacity, 8)
        self.assertEqual(self.pool.operational_target, 3)

    def test_operational_target_cannot_exceed_physical_capacity(self):
        invalid_pool = ResourcePool(
            code="INVALID",
            name="invalid",
            resource_type="rTMS_private",
            physical_capacity=3,
            operational_target=4,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                invalid_pool.save()

    def test_planned_and_actual_discharge_are_independent(self):
        plan = InpatientPlan.objects.create(
            treatment_course=self.course_one,
            estimated_discharge_date=date(2026, 11, 1),
            planned_discharge_date=date(2026, 11, 10),
        )
        self.assertEqual(resolve_inpatient_end_date(plan), (date(2026, 11, 10), "planned"))
        plan.actual_discharge_at = datetime(2026, 11, 12, 9, 30, tzinfo=dt_timezone.utc)
        plan.save(update_fields=["actual_discharge_at", "updated_at"])
        self.assertEqual(resolve_inpatient_end_date(plan), (date(2026, 11, 12), "actual"))
        self.course_one.refresh_from_db()
        self.assertEqual(self.course_one.discharge_date, date(2026, 12, 31))

    def test_assignment_is_course_isolated_and_supports_multiple_periods(self):
        first = ResourceAssignment.objects.create(
            treatment_course=self.course_one,
            resource_pool=self.pool,
            planned_start_date=date(2026, 10, 1),
            planned_end_date=date(2026, 10, 10),
        )
        second = ResourceAssignment.objects.create(
            treatment_course=self.course_one,
            resource_pool=self.pool,
            planned_start_date=date(2026, 10, 11),
            planned_end_date=date(2026, 10, 20),
        )
        other = ResourceAssignment.objects.create(
            treatment_course=self.course_two,
            resource_pool=self.pool,
            planned_start_date=date(2026, 10, 1),
            planned_end_date=date(2026, 10, 10),
        )
        self.assertEqual(self.course_one.resource_assignments.count(), 2)
        self.assertNotEqual(first.treatment_course_id, other.treatment_course_id)
        self.assertEqual(second.planned_end_date, date(2026, 10, 20))

    def test_capacity_check_and_assignment_creation_are_atomic(self):
        for index in range(5):
            course = self.course_one if index == 0 else TreatmentCourse.objects.create(
                patient=Patient.objects.create(
                    card_id=f"8100{index + 2}",
                    name=f"Capacity {index}",
                    birth_date=date(1980, 1, 1),
                ),
                course_number=1,
            )
            create_planned_assignment(
                treatment_course=course,
                resource_pool=self.pool,
                planned_start_date=date(2026, 10, 1),
                planned_end_date=date(2026, 10, 10),
                user=self.user,
            )
        self.assertEqual(
            capacity_snapshot(self.pool, date(2026, 10, 1), date(2026, 10, 10))["planned_count"],
            5,
        )
        with self.assertRaises(ValueError):
            create_planned_assignment(
                treatment_course=self.course_two,
                resource_pool=self.pool,
                planned_start_date=date(2026, 10, 1),
                planned_end_date=date(2026, 10, 10),
                user=self.user,
            )
        self.assertEqual(ResourceAssignment.objects.count(), 5)

    def test_get_or_create_plan_does_not_copy_legacy_dates(self):
        self.course_one.admission_date = date(2026, 9, 1)
        self.course_one.discharge_date = date(2026, 9, 30)
        self.course_one.save(update_fields=["admission_date", "discharge_date", "updated_at"])
        plan = get_or_create_inpatient_plan(treatment_course=self.course_one, user=self.user)
        self.assertIsNone(plan.planned_admission_date)
        self.assertIsNone(plan.planned_discharge_date)
        self.assertIsNone(plan.actual_discharge_at)
