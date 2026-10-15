from datetime import date
from unittest.mock import patch

from django.db import IntegrityError
from django.core.exceptions import ValidationError
from django.test import TransactionTestCase

from rtms_app.models import MappingSchedule, Patient, TreatmentCourse
from rtms_app.services import strict_writes
from rtms_app.services.strict_writes import (
    MappingScheduleWriteError,
    update_or_create_mapping_schedule_legacy,
    update_or_create_mapping_schedule_strict,
)


class MappingScheduleWriteContractTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.patient = Patient.objects.create(
            card_id="MUTEX001",
            name="Mapping Mutex Patient",
            birth_date=date(1980, 1, 1),
            course_number=1,
        )
        self.course_one = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=1,
        )
        self.course_two = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=2,
        )

    def make_legacy_schedule(self, *, patient=None, course_number=1, week_number=1):
        return MappingSchedule.objects.create(
            patient=patient or self.patient,
            treatment_course=None,
            course_number=course_number,
            week_number=week_number,
            planned_date=date(2026, 9, 7),
        )

    def test_strict_write_creates_and_updates_course_scoped_schedule(self):
        schedule, created = update_or_create_mapping_schedule_strict(
            self.patient,
            self.course_one,
            week_number=1,
            planned_date=date(2026, 9, 7),
            course_number=1,
        )
        self.assertTrue(created)
        self.assertEqual(schedule.treatment_course_id, self.course_one.pk)

        updated, created = update_or_create_mapping_schedule_strict(
            self.patient,
            self.course_one,
            week_number=1,
            planned_date=date(2026, 9, 8),
            course_number=1,
        )
        self.assertFalse(created)
        self.assertEqual(updated.pk, schedule.pk)
        self.assertEqual(updated.planned_date, date(2026, 9, 8))
        self.assertEqual(updated.treatment_course_id, self.course_one.pk)

    def test_explicit_legacy_api_writes_only_when_course_is_absent(self):
        patient = Patient.objects.create(
            card_id="MUTEX002",
            name="Legacy Mapping Patient",
            birth_date=date(1981, 1, 1),
            course_number=1,
        )
        schedule, created = update_or_create_mapping_schedule_legacy(
            patient,
            1,
            week_number=1,
            planned_date=date(2026, 9, 7),
        )
        self.assertTrue(created)
        self.assertIsNone(schedule.treatment_course_id)

    def test_legacy_api_routes_to_strict_when_course_exists(self):
        schedule, created = update_or_create_mapping_schedule_legacy(
            self.patient,
            1,
            week_number=1,
            planned_date=date(2026, 9, 7),
        )
        self.assertTrue(created)
        self.assertEqual(schedule.treatment_course_id, self.course_one.pk)

    def test_legacy_absent_course_rejects_mismatched_course_fk_without_changes(self):
        patient = Patient.objects.create(
            card_id="MUTEX004",
            name="Mismatched Mapping Patient",
            birth_date=date(1983, 1, 1),
        )
        unrelated_course = TreatmentCourse.objects.create(
            patient=patient,
            course_number=2,
        )
        schedule = MappingSchedule.objects.create(
            patient=patient,
            treatment_course=unrelated_course,
            course_number=1,
            week_number=1,
            planned_date=date(2026, 9, 7),
        )
        before = MappingSchedule.objects.filter(pk=schedule.pk).values().get()

        with self.assertRaises(MappingScheduleWriteError):
            update_or_create_mapping_schedule_legacy(
                patient,
                1,
                week_number=1,
                planned_date=date(2026, 9, 8),
            )

        self.assertEqual(MappingSchedule.objects.filter(pk=schedule.pk).values().get(), before)

    def test_explicit_course_missing_or_mismatched_never_falls_back_to_legacy(self):
        missing_course = TreatmentCourse(
            pk=999999,
            patient=self.patient,
            course_number=1,
        )
        with self.assertRaises(MappingScheduleWriteError):
            update_or_create_mapping_schedule_strict(
                self.patient,
                missing_course,
                week_number=1,
                planned_date=date(2026, 9, 7),
            )
        with self.assertRaises(ValidationError):
            update_or_create_mapping_schedule_strict(
                self.patient,
                self.course_two,
                course_number=1,
                week_number=1,
                planned_date=date(2026, 9, 7),
            )
        self.assertFalse(MappingSchedule.objects.filter(
            patient=self.patient,
            course_number=1,
            week_number=1,
            treatment_course__isnull=True,
        ).exists())

    def test_legacy_route_does_not_demote_course_scoped_schedule(self):
        schedule = MappingSchedule.objects.create(
            patient=self.patient,
            treatment_course=self.course_one,
            course_number=1,
            week_number=1,
            planned_date=date(2026, 9, 7),
        )
        result, created = update_or_create_mapping_schedule_legacy(
            self.patient,
            1,
            week_number=1,
            planned_date=date(2026, 9, 8),
        )
        schedule.refresh_from_db()
        self.assertFalse(created)
        self.assertEqual(result.pk, schedule.pk)
        self.assertEqual(schedule.treatment_course_id, self.course_one.pk)
        self.assertEqual(schedule.planned_date, date(2026, 9, 8))
        self.assertFalse(MappingSchedule.objects.filter(
            patient=self.patient,
            course_number=1,
            week_number=1,
            treatment_course__isnull=True,
        ).exists())

    def test_unique_exact_legacy_row_is_promoted_without_other_field_changes(self):
        schedule = self.make_legacy_schedule()
        before = MappingSchedule.objects.filter(pk=schedule.pk).values().get()

        promoted, created = update_or_create_mapping_schedule_strict(
            self.patient,
            self.course_one,
            course_number=1,
            week_number=1,
            planned_date=schedule.planned_date,
        )

        after = MappingSchedule.objects.filter(pk=schedule.pk).values().get()
        self.assertFalse(created)
        self.assertEqual(promoted.pk, schedule.pk)
        self.assertEqual(after["treatment_course_id"], self.course_one.pk)
        for field in ("patient_id", "course_number", "week_number", "planned_date", "updated_at"):
            self.assertEqual(after[field], before[field])

    def test_ambiguous_or_duplicate_candidates_fail_without_changes(self):
        legacy = self.make_legacy_schedule()
        strict = MappingSchedule(
            patient=self.patient,
            treatment_course=self.course_one,
            course_number=1,
            week_number=1,
            planned_date=date(2026, 9, 7),
        )
        legacy_snapshot = MappingSchedule.objects.filter(pk=legacy.pk).values().get()
        course_snapshot = TreatmentCourse.objects.filter(pk=self.course_one.pk).values().get()
        ambiguous_cases = (
            ([strict], [legacy]),
            ([], [legacy, legacy]),
        )
        for strict_rows, legacy_rows in ambiguous_cases:
            with self.subTest(strict_rows=bool(strict_rows), legacy_count=len(legacy_rows)):
                with patch.object(
                    strict_writes,
                    "_load_mapping_schedule_candidates",
                    return_value=(strict_rows, legacy_rows),
                ):
                    with self.assertRaises(MappingScheduleWriteError):
                        update_or_create_mapping_schedule_strict(
                            self.patient,
                            self.course_one,
                            course_number=1,
                            week_number=1,
                            planned_date=date(2026, 9, 8),
                        )
                self.assertEqual(
                    MappingSchedule.objects.filter(pk=legacy.pk).values().get(),
                    legacy_snapshot,
                )
                self.assertIsNone(strict.pk)
                self.assertEqual(
                    TreatmentCourse.objects.filter(pk=self.course_one.pk).values().get(),
                    course_snapshot,
                )

    def test_legacy_owner_or_course_number_mismatch_fails_closed(self):
        other_patient = Patient.objects.create(
            card_id="MUTEX003",
            name="Other Mapping Patient",
            birth_date=date(1982, 1, 1),
        )
        foreign_schedule = self.make_legacy_schedule(patient=other_patient)
        course_two_schedule = self.make_legacy_schedule(course_number=2)
        for candidate in (foreign_schedule, course_two_schedule):
            with self.subTest(patient_id=candidate.patient_id, course_number=candidate.course_number):
                before = MappingSchedule.objects.filter(pk=candidate.pk).values().get()
                with patch.object(
                    strict_writes,
                    "_load_mapping_schedule_candidates",
                    return_value=([], [candidate]),
                ):
                    with self.assertRaises(MappingScheduleWriteError):
                        update_or_create_mapping_schedule_strict(
                            self.patient,
                            self.course_one,
                            course_number=1,
                            week_number=1,
                            planned_date=date(2026, 9, 8),
                        )
                self.assertEqual(
                    MappingSchedule.objects.filter(pk=candidate.pk).values().get(),
                    before,
                )

    def test_strict_candidate_owner_course_or_week_mismatch_fails_closed(self):
        other_patient = Patient.objects.create(
            card_id="MUTEX005",
            name="Mismatched Strict Patient",
            birth_date=date(1984, 1, 1),
        )
        candidates = (
            MappingSchedule(
                patient=other_patient,
                treatment_course=self.course_one,
                course_number=1,
                week_number=1,
                planned_date=date(2026, 9, 7),
            ),
            MappingSchedule(
                patient=self.patient,
                treatment_course=self.course_one,
                course_number=2,
                week_number=1,
                planned_date=date(2026, 9, 7),
            ),
            MappingSchedule(
                patient=self.patient,
                treatment_course=self.course_one,
                course_number=1,
                week_number=2,
                planned_date=date(2026, 9, 7),
            ),
        )
        for candidate in candidates:
            with self.subTest(
                patient_id=candidate.patient_id,
                course_number=candidate.course_number,
                week_number=candidate.week_number,
            ):
                with patch.object(
                    strict_writes,
                    "_load_mapping_schedule_candidates",
                    return_value=([candidate], []),
                ):
                    with self.assertRaises(MappingScheduleWriteError):
                        update_or_create_mapping_schedule_strict(
                            self.patient,
                            self.course_one,
                            course_number=1,
                            week_number=1,
                            planned_date=date(2026, 9, 8),
                        )
                self.assertIsNone(candidate.pk)

    def test_course_one_and_two_schedules_remain_separate(self):
        first, _ = update_or_create_mapping_schedule_strict(
            self.patient,
            self.course_one,
            course_number=1,
            week_number=1,
            planned_date=date(2026, 9, 7),
        )
        second, _ = update_or_create_mapping_schedule_strict(
            self.patient,
            self.course_two,
            course_number=2,
            week_number=1,
            planned_date=date(2026, 9, 14),
        )
        self.assertNotEqual(first.pk, second.pk)
        self.assertEqual(first.treatment_course_id, self.course_one.pk)
        self.assertEqual(second.treatment_course_id, self.course_two.pk)
        self.assertEqual(first.course_number, 1)
        self.assertEqual(second.course_number, 2)

    def test_integrity_error_after_promotion_rolls_back_entire_transaction(self):
        schedule = self.make_legacy_schedule()
        before = MappingSchedule.objects.filter(pk=schedule.pk).values().get()
        course_before = TreatmentCourse.objects.filter(pk=self.course_one.pk).values().get()

        with patch.object(
            strict_writes,
            "_update_mapping_schedule_date",
            side_effect=IntegrityError("simulated unique constraint conflict"),
        ):
            with self.assertRaises(IntegrityError):
                update_or_create_mapping_schedule_strict(
                    self.patient,
                    self.course_one,
                    course_number=1,
                    week_number=1,
                    planned_date=date(2026, 9, 8),
                )

        self.assertEqual(MappingSchedule.objects.filter(pk=schedule.pk).values().get(), before)
        self.assertEqual(
            TreatmentCourse.objects.filter(pk=self.course_one.pk).values().get(),
            course_before,
        )
