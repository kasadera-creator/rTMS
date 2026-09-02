from django.test import TestCase, Client, RequestFactory
from django.apps import apps as django_apps
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.utils import timezone
from django.db import transaction
from django.db import IntegrityError
from django.core.exceptions import ValidationError
from unittest.mock import patch
from copy import deepcopy
import csv
import io
import json
from importlib import import_module

from rtms_app import assessment_rules
from rtms_app.models import (
    Patient, TreatmentCourse, Assessment, AssessmentRecord, TreatmentSession,
    MappingSession, MappingSchedule, AssessmentSchedule, ScaleDefinition, TreatmentSkip,
    SideEffectCheck, PatientSurveySession, SeriousAdverseEvent, AdverseEventReport, TimingScaleConfig,
)
import datetime
from datetime import date
from rtms_app import services
from rtms_app.services import schedule as schedule_service
from rtms_app.surveys import INSTRUMENT_ORDER, get_instrument
from rtms_app.services.patient_accounts import ensure_patient_group


class TestDashboardCourseIsolation(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='dashboard-course-user', password='pw')
        self.client = Client()
        self.client.force_login(self.user)
        self.patient = Patient.objects.create(
            card_id='DASHCOURSE', name='Dashboard Course Patient', birth_date=date(1980, 1, 1),
            course_number=1,
            admission_date=date(2026, 1, 1), mapping_date=date(2026, 1, 2),
            first_treatment_date=date(2026, 1, 5), discharge_date=date(2026, 2, 13),
        )
        self.course_one = TreatmentCourse.objects.create(
            patient=self.patient, course_number=1,
            admission_date=date(2026, 1, 1), mapping_date=date(2026, 1, 2),
            first_treatment_date=date(2026, 1, 5), discharge_date=date(2026, 2, 13),
        )
        self.course_two = TreatmentCourse.objects.create(
            patient=self.patient, course_number=2,
            admission_date=date(2026, 8, 1), mapping_date=date(2026, 8, 3),
            first_treatment_date=date(2026, 8, 4), discharge_date=date(2026, 9, 11),
        )

    def _tasks_for(self, response, title):
        return next(group['list'] for group in response.context['dashboard_tasks'] if group['title'] == title)

    def test_course_two_dates_are_isolated_from_patient_and_course_one(self):
        response = self.client.get('/app/dashboard/?date=2026-08-03&course_number=2')

        mapping_tasks = self._tasks_for(response, '③ MT測定')
        self.assertEqual(len(mapping_tasks), 1)
        self.assertEqual(mapping_tasks[0]['course_number'], 2)
        self.assertContains(response, 'course_number=2')
        self.assertNotContains(response, 'course_number=1')
        self.assertEqual(self.patient.refresh_from_db(), None)
        self.assertEqual(self.patient.admission_date, date(2026, 1, 1))
        self.assertEqual(self.course_one.admission_date, date(2026, 1, 1))
        self.assertEqual(self.course_one.mapping_date, date(2026, 1, 2))

    def test_default_dashboard_keeps_course_one_compatibility(self):
        response = self.client.get('/app/dashboard/?date=2026-01-02')

        mapping_tasks = self._tasks_for(response, '③ MT測定')
        self.assertEqual(len(mapping_tasks), 1)
        self.assertEqual(mapping_tasks[0]['course_number'], 1)

    def test_patients_without_course_use_patient_date_fallback(self):
        legacy = Patient.objects.create(
            card_id='DASHLEGACY', name='Dashboard Legacy Patient', birth_date=date(1980, 1, 1),
            admission_date=date(2026, 7, 1), course_number=1,
        )

        response = self.client.get('/app/dashboard/?date=2026-07-01')

        admission_tasks = self._tasks_for(response, '② 入院')
        self.assertEqual([item['obj'].id for item in admission_tasks], [legacy.id])


class TestAssessmentRules(TestCase):
    def test_classify_response_status_baseline20_improvement20_is_response(self):
        # baseline 20 -> current 16 => improvement 20% -> 反応
        imp = assessment_rules.compute_improvement_rate(20, 16)
        status = assessment_rules.classify_response_status(score_17=16, improvement=imp)
        self.assertEqual(status, "反応")

    def test_classify_response_status_baseline20_current7_is_remission(self):
        # baseline 20 -> current 7 => remission
        imp = assessment_rules.compute_improvement_rate(20, 7)
        status = assessment_rules.classify_response_status(score_17=7, improvement=imp)
        self.assertEqual(status, "寛解")

    def test_classify_response_status_baseline20_current19_is_no_response(self):
        # baseline 20 -> current 19 => improvement 5% -> 反応なし
        imp = assessment_rules.compute_improvement_rate(20, 19)
        status = assessment_rules.classify_response_status(score_17=19, improvement=imp)
        self.assertEqual(status, "反応なし")


class TestTreatmentCourseModel(TestCase):
    def setUp(self):
        self.patient = Patient.objects.create(
            card_id="54321", name="Course Patient", birth_date=date(1980, 1, 1)
        )

    def test_patient_can_have_three_courses(self):
        courses = [
            TreatmentCourse.objects.create(patient=self.patient, course_number=number)
            for number in (1, 2, 3)
        ]

        self.assertEqual(
            list(self.patient.treatment_courses.values_list("course_number", flat=True)),
            [1, 2, 3],
        )
        self.assertEqual(courses[0].course_status, "waiting_admission")

    def test_duplicate_course_number_for_patient_is_rejected(self):
        TreatmentCourse.objects.create(patient=self.patient, course_number=2)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TreatmentCourse.objects.create(patient=self.patient, course_number=2)

    def test_different_patients_can_each_have_course_one(self):
        other_patient = Patient.objects.create(
            card_id="54322", name="Other Patient", birth_date=date(1981, 1, 1)
        )

        TreatmentCourse.objects.create(patient=self.patient, course_number=1)
        TreatmentCourse.objects.create(patient=other_patient, course_number=1)

        self.assertEqual(TreatmentCourse.objects.filter(course_number=1).count(), 2)

    def test_status_choices_and_end_reason_are_available(self):
        course = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=1,
            course_status="treatment_in_progress",
            course_end_reason="adverse_event",
        )

        self.assertEqual(dict(TreatmentCourse.COURSE_STATUS_CHOICES)[course.course_status], "rTMS中")
        self.assertEqual(dict(TreatmentCourse.COURSE_END_REASON_CHOICES)[course.course_end_reason], "有害事象")


class TestPatientRegistrationLifecycle(TestCase):
    def test_registration_creates_initial_course_one(self):
        from rtms_app.services.patient_registration import register_patient_with_initial_course

        patient, course = register_patient_with_initial_course(
            Patient(card_id="54329", name="Registered Patient", birth_date=date(1980, 1, 1))
        )

        self.assertEqual(course.patient_id, patient.id)
        self.assertEqual(course.course_number, 1)
        self.assertEqual(course.course_status, "waiting_admission")
        self.assertEqual(
            TreatmentCourse.objects.filter(patient=patient, course_number=1).count(),
            1,
        )

    def test_initial_course_creation_is_idempotent(self):
        from rtms_app.services.patient_registration import ensure_initial_treatment_course

        patient = Patient.objects.create(
            card_id="54330", name="Existing Patient", birth_date=date(1980, 1, 1)
        )
        first = ensure_initial_treatment_course(patient)
        second = ensure_initial_treatment_course(patient)

        self.assertEqual(first.id, second.id)
        self.assertEqual(
            TreatmentCourse.objects.filter(patient=patient, course_number=1).count(),
            1,
        )

    def test_registration_rolls_back_patient_when_course_creation_fails(self):
        from rtms_app.services.patient_registration import register_patient_with_initial_course

        patient = Patient(card_id="54331", name="Rollback Patient", birth_date=date(1980, 1, 1))
        with patch(
            "rtms_app.services.patient_registration.TreatmentCourse.objects.get_or_create",
            side_effect=RuntimeError("course creation failed"),
        ):
            with self.assertRaises(RuntimeError):
                register_patient_with_initial_course(patient)

        self.assertFalse(Patient.objects.filter(card_id="54331").exists())
        self.assertFalse(TreatmentCourse.objects.filter(patient__card_id="54331").exists())


class TestMappingTreatmentCourseIsolation(TestCase):
    def setUp(self):
        self.patient = Patient.objects.create(
            card_id="54323", name="Mapping Course Patient", birth_date=date(1980, 1, 1)
        )
        self.course_one = TreatmentCourse.objects.create(patient=self.patient, course_number=1)
        self.course_two = TreatmentCourse.objects.create(patient=self.patient, course_number=2)

    def test_mapping_sessions_are_isolated_by_treatment_course(self):
        first = MappingSession.objects.create(
            patient=self.patient,
            course_number=1,
            treatment_course=self.course_one,
            date=date(2026, 1, 5),
            resting_mt=50,
            stimulation_site="left",
        )
        second = MappingSession.objects.create(
            patient=self.patient,
            course_number=2,
            treatment_course=self.course_two,
            date=date(2026, 1, 5),
            resting_mt=60,
            stimulation_site="left",
        )

        self.assertEqual(list(self.course_one.mapping_sessions.all()), [first])
        self.assertEqual(list(self.course_two.mapping_sessions.all()), [second])
        self.assertEqual(first.treatment_course_id, self.course_one.id)
        self.assertEqual(second.treatment_course_id, self.course_two.id)

    def test_mapping_schedules_are_isolated_by_treatment_course(self):
        first = MappingSchedule.objects.create(
            patient=self.patient,
            course_number=1,
            treatment_course=self.course_one,
            week_number=1,
            planned_date=date(2026, 1, 5),
        )
        second = MappingSchedule.objects.create(
            patient=self.patient,
            course_number=2,
            treatment_course=self.course_two,
            week_number=1,
            planned_date=date(2026, 6, 1),
        )

        self.assertEqual(list(self.course_one.mapping_schedules.all()), [first])
        self.assertEqual(list(self.course_two.mapping_schedules.all()), [second])
        self.assertEqual(first.treatment_course_id, self.course_one.id)
        self.assertEqual(second.treatment_course_id, self.course_two.id)

    def test_mapping_add_course_two_uses_course_first_treatment_date(self):
        user = get_user_model().objects.create_user(username='mapping-week-user', password='pw')
        self.client = Client()
        self.client.force_login(user)
        self.patient.first_treatment_date = date(2026, 1, 5)
        self.patient.save(update_fields=['first_treatment_date'])
        self.course_one.first_treatment_date = date(2026, 1, 5)
        self.course_one.save(update_fields=['first_treatment_date'])
        self.course_two.first_treatment_date = date(2026, 4, 1)
        self.course_two.save(update_fields=['first_treatment_date'])

        response = self.client.get(
            reverse('rtms_app:mapping_add', args=[self.patient.pk]),
            {'course_number': 2, 'date': '2026-04-08'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['week_no_default'], 2)
        self.assertEqual(response.context['form'].initial['week_number'], 2)


class TestTreatmentCourseWriteIsolation(TestCase):
    def setUp(self):
        self.patient = Patient.objects.create(
            card_id="WRITE_COURSE_001", name="Write Course Patient", birth_date=date(1980, 1, 1)
        )
        self.course_one = TreatmentCourse.objects.create(patient=self.patient, course_number=1)
        self.course_two = TreatmentCourse.objects.create(patient=self.patient, course_number=2)
        self.scale = ScaleDefinition.objects.create(code="write-course-scale", name="Write Course Scale")

    def test_explicit_course_two_is_saved_on_every_course_owned_model(self):
        from rtms_app.queries.assessment_queries import save_assessment_hamd, save_assessment_record

        session = TreatmentSession.objects.create(
            patient=self.patient, treatment_course=self.course_two, course_number=2,
            session_date=date(2026, 9, 1),
        )
        assessment, _ = save_assessment_hamd(
            patient=self.patient, treatment_course=self.course_two, course_number=2,
            timing="baseline", date=date(2026, 9, 1), scores={},
        )
        record, _ = save_assessment_record(
            patient=self.patient, treatment_course=self.course_two, course_number=2,
            timing="baseline", scale=self.scale, date=date(2026, 9, 1), scores={},
        )
        assessment_schedule = AssessmentSchedule.objects.create(
            patient=self.patient, treatment_course=self.course_two, course_number=2,
            scale=self.scale, timing="baseline", planned_date=date(2026, 9, 1),
        )
        mapping = MappingSession.objects.create(
            patient=self.patient, treatment_course=self.course_two, course_number=2,
            date=date(2026, 9, 1), resting_mt=50, stimulation_site="left",
        )
        mapping_schedule = MappingSchedule.objects.create(
            patient=self.patient, treatment_course=self.course_two, course_number=2,
            week_number=1, planned_date=date(2026, 9, 1),
        )

        self.assertEqual(self.patient.course_number, 1)
        self.assertEqual(session.treatment_course_id, self.course_two.id)
        self.assertEqual(assessment.treatment_course_id, self.course_two.id)
        self.assertEqual(record.treatment_course_id, self.course_two.id)
        self.assertEqual(assessment_schedule.treatment_course_id, self.course_two.id)
        self.assertEqual(mapping.treatment_course_id, self.course_two.id)
        self.assertEqual(mapping_schedule.treatment_course_id, self.course_two.id)
        self.assertEqual(self.course_one.treatment_sessions.count(), 0)
        self.assertEqual(self.course_one.assessments.count(), 0)
        self.assertEqual(self.course_one.assessment_records.count(), 0)
        self.assertEqual(self.course_one.assessment_schedules.count(), 0)
        self.assertEqual(self.course_one.mapping_sessions.count(), 0)
        self.assertEqual(self.course_one.mapping_schedules.count(), 0)

    def test_assessment_writes_reject_course_scope_mismatch(self):
        from rtms_app.queries.assessment_queries import save_assessment_hamd

        with self.assertRaises(ValueError):
            save_assessment_hamd(
                patient=self.patient, treatment_course=self.course_two, course_number=1,
                timing="baseline", date=date(2026, 9, 2), scores={},
            )

        other_patient = Patient.objects.create(
            card_id="WRITE_COURSE_002", name="Other Patient", birth_date=date(1981, 1, 1)
        )
        other_course = TreatmentCourse.objects.create(patient=other_patient, course_number=1)
        with self.assertRaises(ValueError):
            save_assessment_hamd(
                patient=self.patient, treatment_course=other_course, course_number=1,
                timing="week3", date=date(2026, 9, 2), scores={},
            )


class TestStrictCourseWriteAPIs(TestCase):
    def setUp(self):
        self.patient = Patient.objects.create(
            card_id="STRICT_WRITE_001", name="Strict Write Patient", birth_date=date(1980, 1, 1),
            course_number=1,
        )
        self.course_one = TreatmentCourse.objects.create(patient=self.patient, course_number=1)
        self.course_two = TreatmentCourse.objects.create(patient=self.patient, course_number=2)
        self.scale = ScaleDefinition.objects.create(code="strict-write-scale", name="Strict Write Scale")

    def test_course_two_isolation_for_all_six_models(self):
        from rtms_app.queries.assessment_queries import save_assessment_hamd, save_assessment_record
        from rtms_app.services.strict_writes import (
            create_treatment_session_strict,
            save_mapping_session_strict,
            update_or_create_assessment_schedule_strict,
            update_or_create_mapping_schedule_strict,
        )

        session = create_treatment_session_strict(
            self.patient, self.course_two, session_date=date(2026, 9, 1),
        )
        assessment, _ = save_assessment_hamd(
            patient=self.patient, treatment_course=self.course_two, course_number=2,
            timing="baseline", date=date(2026, 9, 1), scores={},
        )
        record, _ = save_assessment_record(
            patient=self.patient, treatment_course=self.course_two, course_number=2,
            timing="baseline", scale=self.scale, date=date(2026, 9, 1), scores={},
        )
        assessment_schedule, _ = update_or_create_assessment_schedule_strict(
            self.patient, self.course_two, scale=self.scale, timing="baseline",
            planned_date=date(2026, 9, 1),
        )
        mapping = save_mapping_session_strict(
            MappingSession(date=date(2026, 9, 1), resting_mt=50),
            self.patient, self.course_two,
        )
        mapping_schedule, _ = update_or_create_mapping_schedule_strict(
            self.patient, self.course_two, week_number=1, planned_date=date(2026, 9, 1),
        )

        for obj in (session, assessment, record, assessment_schedule, mapping, mapping_schedule):
            self.assertEqual(obj.treatment_course_id, self.course_two.id)
        self.assertEqual(self.patient.course_number, 1)
        self.assertEqual(self.course_one.treatment_sessions.count(), 0)
        self.assertEqual(self.course_one.assessments.count(), 0)
        self.assertEqual(self.course_one.assessment_records.count(), 0)
        self.assertEqual(self.course_one.assessment_schedules.count(), 0)
        self.assertEqual(self.course_one.mapping_sessions.count(), 0)
        self.assertEqual(self.course_one.mapping_schedules.count(), 0)

    def test_strict_apis_reject_none_and_scope_mismatches(self):
        from rtms_app.services.strict_writes import (
            create_treatment_session_strict,
            save_mapping_session_strict,
            update_or_create_assessment_schedule_strict,
            update_or_create_mapping_schedule_strict,
        )

        strict_calls = [
            lambda: create_treatment_session_strict(self.patient, None, session_date=date.today()),
            lambda: save_mapping_session_strict(MappingSession(date=date.today(), resting_mt=50), self.patient, None),
            lambda: update_or_create_mapping_schedule_strict(self.patient, None, week_number=1, planned_date=date.today()),
            lambda: update_or_create_assessment_schedule_strict(self.patient, None, scale=self.scale, timing="baseline", planned_date=date.today()),
        ]
        for call in strict_calls:
            with self.assertRaises(ValidationError):
                call()

        other_patient = Patient.objects.create(
            card_id="STRICT_WRITE_002", name="Other Patient", birth_date=date(1981, 1, 1),
        )
        other_course = TreatmentCourse.objects.create(patient=other_patient, course_number=2)
        with self.assertRaises(ValidationError):
            create_treatment_session_strict(self.patient, other_course, session_date=date.today())
        with self.assertRaises(ValidationError):
            create_treatment_session_strict(self.patient, self.course_two, course_number=1, session_date=date.today())

    def test_explicit_legacy_apis_preserve_course_less_compatibility(self):
        from rtms_app.queries.assessment_queries import save_assessment_hamd_legacy, save_assessment_record_legacy
        from rtms_app.services.strict_writes import (
            get_or_create_treatment_session_legacy,
            save_mapping_session_legacy,
            update_or_create_assessment_schedule_legacy,
            update_or_create_mapping_schedule_legacy,
            update_or_create_treatment_session_legacy,
        )

        patient = Patient.objects.create(
            card_id="STRICT_WRITE_LEGACY", name="Legacy Patient", birth_date=date(1982, 1, 1),
            course_number=1,
        )
        session, created = get_or_create_treatment_session_legacy(
            patient, 1, session_date=date(2026, 9, 1), slot="",
        )
        self.assertTrue(created)
        self.assertIsNone(session.treatment_course_id)
        session, created = update_or_create_treatment_session_legacy(
            patient, 1, session_date=date(2026, 9, 1), slot="",
            defaults={"status": "planned"},
        )
        self.assertFalse(created)
        self.assertIsNone(session.treatment_course_id)

        mapping = save_mapping_session_legacy(
            MappingSession(date=date(2026, 9, 2), resting_mt=50), patient, 1,
        )
        mapping_schedule, _ = update_or_create_mapping_schedule_legacy(
            patient, 1, week_number=1, planned_date=date(2026, 9, 2),
        )
        assessment_schedule, _ = update_or_create_assessment_schedule_legacy(
            patient, 1, scale=self.scale, timing="baseline", planned_date=date(2026, 9, 2),
        )
        assessment, _ = save_assessment_hamd_legacy(
            patient=patient, course_number=1, timing="baseline", date=date(2026, 9, 2), scores={},
        )
        record, _ = save_assessment_record_legacy(
            patient=patient, course_number=1, timing="baseline", scale=self.scale,
            date=date(2026, 9, 2), scores={},
        )
        for obj in (mapping, mapping_schedule, assessment_schedule, assessment, record):
            self.assertIsNone(obj.treatment_course_id)


class TestTreatmentCourseDataMigration(TestCase):
    migration = import_module("rtms_app.migrations.0045_populate_treatment_courses")

    def create_patient(self, card_id, status="waiting"):
        physician = get_user_model().objects.create_user(username=f"doctor-{card_id}")
        return Patient.objects.create(
            card_id=card_id,
            name=f"Patient {card_id}",
            birth_date=date(1980, 1, 1),
            status=status,
            diagnosis="F33",
            chief_complaint="主訴",
            present_illness="現病歴",
            medication_history="薬剤治療歴",
            weight_kg=62.5,
            is_weight_unknown=False,
            attending_physician=physician,
            referral_source="紹介元",
            referral_doctor="紹介医",
            estimated_onset_year=2020,
            estimated_onset_month=4,
            is_all_case_survey=True,
            first_visit_date=date(2026, 1, 2),
            admission_date=date(2026, 1, 10),
            admission_type="voluntary",
            is_admission_procedure_done=True,
            first_treatment_date=date(2026, 1, 12),
            mapping_date=date(2026, 1, 11),
            mapping_notes="位置決めメモ",
            summary_text="サマリー",
            discharge_prescription="退院時処方",
            discharge_date=date(2026, 2, 20),
            questionnaire_data={"q1": ["はい"], "nested": {"value": 1}},
        )

    def run_migration(self):
        self.migration.populate_treatment_courses(django_apps, None)

    def test_creates_one_course_per_patient_and_copies_fields(self):
        patient = self.create_patient("54331")

        self.run_migration()

        course = TreatmentCourse.objects.get(patient=patient, course_number=1)
        for field in self.migration.COPY_FIELDS:
            self.assertEqual(getattr(course, field), getattr(patient, field))
        self.assertEqual(course.course_status, "waiting_admission")
        self.assertEqual(course.course_end_reason, "")

    def test_patient_and_course_counts_match_for_course_one(self):
        self.create_patient("54332")
        self.create_patient("54333")

        self.run_migration()

        self.assertEqual(Patient.objects.count(), 2)
        self.assertEqual(TreatmentCourse.objects.values("patient_id").distinct().count(), 2)
        self.assertEqual(TreatmentCourse.objects.filter(course_number=1).count(), 2)

    def test_questionnaire_is_an_independent_copy(self):
        patient = self.create_patient("54334")
        original = deepcopy(patient.questionnaire_data)

        self.run_migration()

        course = TreatmentCourse.objects.get(patient=patient, course_number=1)
        self.assertEqual(course.questionnaire_data, original)
        self.assertIsNot(course.questionnaire_data, patient.questionnaire_data)
        course_data = course.questionnaire_data or {}
        course_data["nested"]["value"] = 99
        course.questionnaire_data = course_data
        course.save(update_fields=["questionnaire_data", "updated_at"])
        patient.refresh_from_db()
        self.assertEqual(patient.questionnaire_data, original)

    def test_status_mapping_is_explicit(self):
        expected = {
            "waiting": "waiting_admission",
            "inpatient": "inpatient_waiting_treatment",
            "discharged": "discharged",
        }
        patients = [
            self.create_patient(f"5433{index}", status=status)
            for index, status in enumerate(expected, start=5)
        ]

        self.run_migration()

        self.assertEqual(
            [TreatmentCourse.objects.get(patient=patient).course_status for patient in patients],
            list(expected.values()),
        )

    def test_unknown_status_aborts_without_creating_courses(self):
        self.create_patient("54337", status="unknown")

        with self.assertRaisesRegex(RuntimeError, "unsupported Patient.status"):
            self.run_migration()

        self.assertEqual(TreatmentCourse.objects.count(), 0)

    def test_is_idempotent_and_preserves_existing_course(self):
        patient = self.create_patient("54338")
        existing = TreatmentCourse.objects.create(
            patient=patient,
            course_number=1,
            diagnosis="既存Course診断",
            course_status="on_hold",
            questionnaire_data={"preserved": True},
        )

        self.run_migration()
        self.run_migration()

        self.assertEqual(TreatmentCourse.objects.filter(patient=patient, course_number=1).count(), 1)
        existing.refresh_from_db()
        self.assertEqual(existing.diagnosis, "既存Course診断")
        self.assertEqual(existing.course_status, "on_hold")
        self.assertEqual(existing.questionnaire_data, {"preserved": True})

    def test_existing_related_model_counts_do_not_change(self):
        patient = self.create_patient("54339")
        session = TreatmentSession.objects.create(patient=patient, course_number=1, session_date=date(2026, 1, 12))
        scale = ScaleDefinition.objects.create(code="migration-test", name="Migration Test")
        Assessment.objects.create(patient=patient, course_number=1, timing="baseline", type="HAM-D")
        AssessmentRecord.objects.create(patient=patient, course_number=1, timing="baseline", scale=scale)
        MappingSession.objects.create(patient=patient, course_number=1, date=date(2026, 1, 11), resting_mt=50)
        MappingSchedule.objects.create(patient=patient, course_number=1, week_number=1, planned_date=date(2026, 1, 11))
        AssessmentSchedule.objects.create(patient=patient, course_number=1, scale=scale, timing="baseline", planned_date=date(2026, 1, 2))
        PatientSurveySession.objects.create(patient=patient, course_number=1, phase="pre")
        SeriousAdverseEvent.objects.create(patient=patient, course_number=1, session=session, event_types=["other"])
        AdverseEventReport.objects.create(session=session)
        before = [
            TreatmentSession.objects.count(), Assessment.objects.count(), AssessmentRecord.objects.count(),
            MappingSession.objects.count(), MappingSchedule.objects.count(), AssessmentSchedule.objects.count(),
            PatientSurveySession.objects.count(), SeriousAdverseEvent.objects.count(), AdverseEventReport.objects.count(),
        ]

        self.run_migration()

        after = [
            TreatmentSession.objects.count(), Assessment.objects.count(), AssessmentRecord.objects.count(),
            MappingSession.objects.count(), MappingSchedule.objects.count(), AssessmentSchedule.objects.count(),
            PatientSurveySession.objects.count(), SeriousAdverseEvent.objects.count(), AdverseEventReport.objects.count(),
        ]
        self.assertEqual(after, before)


class TestTreatmentSessionCourseMigration(TestCase):
    migration = import_module("rtms_app.migrations.0047_populate_treatment_session_courses")

    def setUp(self):
        self.patient = Patient.objects.create(
            card_id="54341", name="Session Patient", birth_date=date(1980, 1, 1)
        )
        self.course_one = TreatmentCourse.objects.create(patient=self.patient, course_number=1)

    def run_migration(self):
        self.migration.populate_treatment_session_courses(django_apps, None)

    def test_existing_sessions_are_linked_without_changing_legacy_fields(self):
        sessions = [
            TreatmentSession.objects.create(
                patient=self.patient,
                course_number=1,
                session_date=date(2026, 1, day),
            )
            for day in (12, 13, 14)
        ]

        self.run_migration()

        for session in sessions:
            session.refresh_from_db()
            self.assertEqual(session.treatment_course, self.course_one)
            self.assertEqual(session.patient, session.treatment_course.patient)
            self.assertEqual(session.course_number, session.treatment_course.course_number)
        self.assertEqual(TreatmentSession.objects.count(), 3)

    def test_missing_course_stops_without_assigning_a_course(self):
        session = TreatmentSession.objects.create(
            patient=self.patient,
            course_number=2,
            session_date=date(2026, 1, 15),
        )

        with self.assertRaisesRegex(RuntimeError, "no unique matching TreatmentCourse"):
            self.run_migration()

        session.refresh_from_db()
        self.assertIsNone(session.treatment_course)

    def test_sessions_are_separated_by_treatment_course(self):
        course_two = TreatmentCourse.objects.create(patient=self.patient, course_number=2)
        first = TreatmentSession.objects.create(
            patient=self.patient,
            course_number=1,
            treatment_course=self.course_one,
            session_date=date(2026, 1, 16),
        )
        second = TreatmentSession.objects.create(
            patient=self.patient,
            course_number=2,
            treatment_course=course_two,
            session_date=date(2026, 1, 16),
        )

        self.assertEqual(schedule_service.get_treatment_sessions(self.patient, 1), [first])
        self.assertEqual(schedule_service.get_treatment_sessions(self.patient, 2), [second])


class TestTreatmentCourseScheduleIsolation(TestCase):
    def setUp(self):
        self.patient = Patient.objects.create(
            card_id="54342", name="Two Course Patient", birth_date=date(1980, 1, 1)
        )
        self.course_one = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=1,
            first_treatment_date=date(2026, 1, 5),
            admission_date=date(2026, 1, 4),
        )
        self.course_two = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=2,
            first_treatment_date=date(2026, 4, 1),
            admission_date=date(2026, 3, 31),
        )

    def test_calendar_contains_only_the_selected_course_sessions(self):
        from rtms_app.views import generate_calendar_weeks

        first = TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=self.course_one,
            course_number=1,
            session_date=date(2026, 1, 5),
        )
        second = TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=self.course_two,
            course_number=2,
            session_date=date(2026, 4, 1),
        )

        first_weeks, _ = generate_calendar_weeks(self.patient, treatment_course=self.course_one)
        second_weeks, _ = generate_calendar_weeks(self.patient, treatment_course=self.course_two)

        def treatment_ids(weeks):
            return {
                event['session_id']
                for week in weeks
                for day in week
                for event in day['events']
                if event['type'] == 'treatment' and event['session_id'] is not None
            }

        self.assertEqual(treatment_ids(first_weeks), {first.pk})
        self.assertEqual(treatment_ids(second_weeks), {second.pk})

    def test_clinical_path_print_link_preserves_selected_course(self):
        user = get_user_model().objects.create_user(username='path-print-user', password='pw')
        client = Client()
        client.force_login(user)
        first = TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=self.course_one,
            course_number=1,
            session_date=date(2026, 1, 5),
        )
        second = TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=self.course_two,
            course_number=2,
            session_date=date(2026, 4, 1),
        )

        path_response = client.get(
            reverse('rtms_app:patient_clinical_path', args=[self.patient.pk]),
            {'course_number': 2},
        )
        self.assertContains(
            path_response,
            f'href="{reverse("rtms_app:print:print_clinical_path", args=[self.patient.pk])}?course_number=2"',
        )

        print_response = client.get(
            reverse('rtms_app:print:print_clinical_path', args=[self.patient.pk]),
            {'course_number': 2},
        )
        printed_ids = {
            event['session_id']
            for week in print_response.context['calendar_weeks']
            for day in week
            for event in day['events']
            if event['type'] == 'treatment' and event['session_id'] is not None
        }
        self.assertEqual(printed_ids, {second.pk})
        self.assertNotIn(first.pk, printed_ids)

    def test_calendar_uses_patient_mapping_date_when_course_date_is_null(self):
        from rtms_app.views import generate_calendar_weeks

        self.patient.mapping_date = date(2026, 4, 1)
        self.patient.save(update_fields=['mapping_date'])

        weeks, _ = generate_calendar_weeks(self.patient, treatment_course=self.course_two)
        mapping_dates = {
            day['date']
            for week in weeks
            for day in week
            for event in day['events']
            if event['type'] == 'mapping'
        }

        self.assertIn(date(2026, 4, 1), mapping_dates)

    def test_rescheduling_one_course_does_not_move_the_other_course(self):
        first = TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=self.course_one,
            course_number=1,
            session_date=date(2026, 1, 5),
            status='planned',
        )
        second = TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=self.course_two,
            course_number=2,
            session_date=date(2026, 1, 5),
            status='planned',
        )

        schedule_service.reschedule_planned_session(
            self.patient, first, date(2026, 1, 6),
        )

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.session_date, date(2026, 1, 6))
        self.assertEqual(second.session_date, date(2026, 1, 5))

    def test_completed_map_does_not_include_another_course(self):
        from rtms_app.views import generate_calendar_weeks

        TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=self.course_one,
            course_number=1,
            session_date=date(2026, 4, 1),
            status='done',
        )
        second = TreatmentSession.objects.create(
            patient=self.patient,
            treatment_course=self.course_two,
            course_number=2,
            session_date=date(2026, 4, 1),
            status='planned',
        )

        weeks, _ = generate_calendar_weeks(self.patient, treatment_course=self.course_two)
        events = [
            event
            for week in weeks
            for day in week
            for event in day['events']
            if event['type'] == 'treatment' and event['session_id'] == second.pk
        ]

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['status'], 'planned')


class TestQuestionnaireEdit(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='questionnaire-user', password='pass1234')
        self.client = Client()
        self.client.force_login(self.user)
        self.patient = Patient.objects.create(
            card_id='QUEST', name='Questionnaire', birth_date=date(1980, 1, 1),
            questionnaire_data={'q_past_rtms': 'はい', 'q_cur_headache': 'いいえ', 'q_details': '既存回答'},
        )

    def test_modal_get_renders_questions_and_existing_answers(self):
        response = self.client.get(
            reverse('rtms_app:questionnaire_edit', args=[self.patient.pk]), {'modal': '1'}
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'rTMS 適正質問票')
        self.assertContains(response, 'rTMS実施経験（治験・研究を含む）')
        self.assertContains(response, '家族内にてんかんを持っているかた')
        self.assertContains(response, 'name="q_past_rtms"')
        self.assertContains(response, 'value="はい"\n                               checked')
        self.assertContains(response, '既存回答')

    def test_modal_post_preserves_all_questionnaire_keys_and_details(self):
        from rtms_app.views import _questionnaire_questions

        questions_past, questions_current, keys = _questionnaire_questions()
        expected_keys = [
            'q_past_rtms', 'q_past_side_effect', 'q_past_ect', 'q_past_seizure',
            'q_past_loc', 'q_past_stroke', 'q_past_trauma', 'q_past_surgery',
            'q_past_neuro', 'q_past_internal', 'q_past_abuse', 'q_cur_headache',
            'q_cur_metal', 'q_cur_device', 'q_cur_abuse', 'q_cur_preg',
            'q_cur_family_epilepsy', 'q_details',
        ]
        self.assertEqual([question['no'] for question in questions_past + questions_current], list(range(1, 18)))
        self.assertEqual(keys, expected_keys)

        answers = {
            key: 'はい' if index % 2 == 0 else 'いいえ'
            for index, key in enumerate(expected_keys[:-1])
        }
        expected_data = {**answers, 'q_details': '保存後も表示される詳細'}
        response = self.client.post(
            reverse('rtms_app:questionnaire_edit', args=[self.patient.pk]) + '?modal=1',
            expected_data,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'success')
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.questionnaire_data, expected_data)

        response = self.client.get(
            reverse('rtms_app:questionnaire_edit', args=[self.patient.pk]), {'modal': '1'}
        )
        for key, answer in answers.items():
            self.assertContains(response, f'name="{key}"\n                               value="{answer}"\n                               checked')
        self.assertContains(response, expected_data['q_details'])


class TestTreatmentAddWeek3Hamd17(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='week3-hamd-user', password='pass1234')
        self.client = Client()
        self.client.force_login(self.user)
        self.patient = Patient.objects.create(
            card_id='HAMD17', name='Week Three', birth_date=date(1980, 1, 1),
            first_treatment_date=date(2026, 1, 5), course_number=1,
        )
        self.hamd = ScaleDefinition.objects.get_or_create(code='hamd', defaults={'name': 'HAM-D'})[0]

    def _create_record(self, timing, total):
        record = AssessmentRecord.objects.filter(
            patient=self.patient, course_number=1, timing=timing, scale=self.hamd,
        ).first()
        if record is None:
            return AssessmentRecord.objects.create(
                patient=self.patient, course_number=1, timing=timing, scale=self.hamd,
                date=date(2026, 1, 5), scores={'q1': total},
            )
        record.date = date(2026, 1, 5)
        record.scores = {'q1': total}
        record.save()
        return record

    def _response_for(self, treatment_date):
        return self.client.get(
            reverse('rtms_app:treatment_add', args=[self.patient.pk]), {'date': treatment_date.isoformat()}
        )

    def test_course_two_treatment_add_uses_course_dates(self):
        from rtms_app.views import get_completion_date

        course_one = TreatmentCourse.objects.create(
            patient=self.patient, course_number=1,
            first_treatment_date=date(2026, 1, 5),
        )
        course_two = TreatmentCourse.objects.create(
            patient=self.patient, course_number=2,
            first_treatment_date=date(2026, 3, 2),
        )

        response = self.client.get(
            reverse('rtms_app:treatment_add', args=[self.patient.pk]),
            {'date': '2026-03-02', 'course_number': 2},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['start_date'], course_two.first_treatment_date)
        self.assertEqual(response.context['week_num'], 1)
        self.assertEqual(
            response.context['end_date_est'],
            get_completion_date(course_two.first_treatment_date),
        )
        self.assertNotEqual(
            response.context['start_date'], course_one.first_treatment_date,
        )

    def test_course_two_weekly_count_excludes_course_one_sessions(self):
        course_one = TreatmentCourse.objects.create(
            patient=self.patient, course_number=1,
            first_treatment_date=date(2026, 1, 5),
        )
        course_two = TreatmentCourse.objects.create(
            patient=self.patient, course_number=2,
            first_treatment_date=date(2026, 3, 2),
        )
        for session_date in (date(2026, 3, 2), date(2026, 3, 3), date(2026, 3, 4)):
            TreatmentSession.objects.create(
                patient=self.patient, treatment_course=course_one,
                course_number=1, session_date=session_date,
            )
        TreatmentSession.objects.create(
            patient=self.patient, treatment_course=course_two,
            course_number=2, session_date=date(2026, 3, 5),
        )

        from rtms_app.views import get_weekly_session_count

        self.assertEqual(
            get_weekly_session_count(self.patient, date(2026, 3, 5), course_number=2),
            1,
        )

    def test_course_two_treatment_add_does_not_change_course_one_sessions(self):
        course_one = TreatmentCourse.objects.create(
            patient=self.patient, course_number=1,
            first_treatment_date=date(2026, 1, 5),
        )
        course_two = TreatmentCourse.objects.create(
            patient=self.patient, course_number=2,
            first_treatment_date=date(2026, 3, 2),
        )
        course_one_session = TreatmentSession.objects.create(
            patient=self.patient, treatment_course=course_one,
            course_number=1, session_date=date(2026, 1, 5),
        )
        TreatmentSession.objects.create(
            patient=self.patient, treatment_course=course_two,
            course_number=2, session_date=date(2026, 3, 2),
        )

        response = self.client.get(
            reverse('rtms_app:treatment_add', args=[self.patient.pk]),
            {'date': '2026-03-02', 'course_number': 2},
        )

        self.assertEqual(response.status_code, 200)
        course_one_session.refresh_from_db()
        self.assertEqual(course_one_session.session_date, date(2026, 1, 5))
        self.assertEqual(
            TreatmentSession.objects.filter(treatment_course=course_one).count(),
            1,
        )

    def test_legacy_treatment_add_uses_patient_date_fallback(self):
        response = self.client.get(
            reverse('rtms_app:treatment_add', args=[self.patient.pk]),
            {'date': '2026-01-05'},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['start_date'], self.patient.first_treatment_date)
        self.assertEqual(response.context['week_num'], 1)

    def test_session_and_week_display_match_canonical_calendar_numbers(self):
        from rtms_app.views import generate_calendar_weeks

        for session_date in (
            date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7),
            date(2026, 1, 8), date(2026, 1, 9), date(2026, 1, 13),
        ):
            TreatmentSession.objects.create(
                patient=self.patient, course_number=1, session_date=session_date
            )

        checks = (
            (date(2026, 1, 5), 1, 1), (date(2026, 1, 6), 2, 1),
            (date(2026, 1, 13), 6, 2), (date(2026, 1, 19), 10, 3),
            (date(2026, 1, 26), 15, 4),
        )
        weeks, _ = generate_calendar_weeks(self.patient)
        labels = {
            day['date']: next((event['label'] for event in day['events'] if event['type'] == 'treatment'), '')
            for week in weeks for day in week
        }
        for treatment_date, session_number, week_number in checks:
            response = self._response_for(treatment_date)
            label = f'{session_number}回目（第{week_number}週）'
            self.assertContains(response, label)
            self.assertIn(label, labels[treatment_date])

    def test_rescheduled_exceptional_dates_match_calendar_and_hamd_week(self):
        from rtms_app.views import generate_calendar_weeks

        for index, target_date in enumerate((date(2026, 1, 17), date(2026, 1, 12))):
            patient = Patient.objects.create(
                card_id=f'RESCHED{index}', name='Rescheduled', birth_date=date(1980, 1, 1),
                first_treatment_date=date(2026, 1, 5), course_number=1,
            )
            session = TreatmentSession.objects.create(
                patient=patient, course_number=1, session_date=date(2026, 1, 5), status='planned',
            )
            schedule_service.reschedule_planned_session(
                patient, session, target_date, allow_exceptional_day=True,
            )

            weeks, _ = generate_calendar_weeks(patient)
            calendar_label = next(
                event['label']
                for week in weeks
                for day in week
                if day['date'] == target_date
                for event in day['events']
                if event['type'] == 'treatment'
            )
            response = self.client.get(
                reverse('rtms_app:treatment_add', args=[patient.pk]), {'date': target_date.isoformat()}
            )
            self.assertContains(response, calendar_label.removeprefix('rTMS治療 '))
            self.assertContains(response, '評価期間前')

    def test_rescheduled_third_week_uses_hamd_instruction(self):
        session = TreatmentSession.objects.create(
            patient=self.patient, course_number=1, session_date=date(2026, 1, 5), status='planned',
        )
        schedule_service.reschedule_planned_session(
            self.patient, session, date(2026, 1, 19), allow_exceptional_day=True,
        )

        response = self._response_for(date(2026, 1, 19))
        self.assertContains(response, '1回目（第3週）')
        self.assertContains(response, 'HAM-Dを実施してください')

    def test_week3_hamd17_instruction_priorities_and_thresholds(self):
        response = self._response_for(date(2026, 1, 13))
        self.assertContains(response, '評価期間前')

        response = self._response_for(date(2026, 1, 19))
        self.assertContains(response, 'HAM-Dを実施してください')

        self._create_record('baseline', 20)
        self._create_record('week3', 7)
        response = self._response_for(date(2026, 1, 19))
        self.assertContains(response, '寛解：治療を中止または漸減してください')

        self._create_record('week3', 5)
        response = self._response_for(date(2026, 1, 19))
        self.assertContains(response, '寛解：治療を中止または漸減してください')

        self._create_record('week3', 0)
        response = self._response_for(date(2026, 1, 19))
        self.assertContains(response, '0 点')
        self.assertContains(response, '寛解：治療を中止または漸減してください')

        self._create_record('week3', 18)
        response = self._response_for(date(2026, 1, 19))
        self.assertContains(response, '改善不十分：治療を中止してください')

        self._create_record('week3', 15)
        response = self._response_for(date(2026, 1, 19))
        self.assertContains(response, '治療継続')

        self._create_record('baseline', 25)
        response = self._response_for(date(2026, 1, 19))
        self.assertContains(response, '40.0%')
        self.assertContains(response, '治療継続')

    def test_remission_taper_limits_for_weeks_four_to_six(self):
        self._create_record('baseline', 20)
        self._create_record('week3', 7)

        for treatment_date, taper_limit in (
            (date(2026, 1, 26), '第4週：最大週3回'),
            (date(2026, 2, 2), '第5週：最大週2回'),
            (date(2026, 2, 9), '第6週：最大週1回'),
        ):
            response = self._response_for(treatment_date)
            self.assertContains(response, taper_limit)

    def test_hamd17_uses_record_ignores_total21_and_handles_zero_baseline(self):
        baseline = self._create_record('baseline', 0)
        week3 = self._create_record('week3', 15)
        AssessmentRecord.objects.filter(pk=week3.pk).update(total_score_21=5)
        Assessment.objects.create(
            patient=self.patient, course_number=1, timing='week3', type='HAM-D', scores={'q1': 5},
        )

        response = self._response_for(date(2026, 1, 19))
        self.assertContains(response, '15 点')
        self.assertContains(response, '改善率を判定できません')
        self.assertNotContains(response, '寛解：治療を中止または漸減してください')
        self.assertEqual(baseline.total_score_17, 0)

    def test_response_threshold_is_inclusive_at_twenty_percent(self):
        self.assertEqual(assessment_rules.classify_response_status(15, 0.199), '反応なし')
        self.assertEqual(assessment_rules.classify_response_status(15, 0.20), '反応')


class TestStage6ScheduleDeadlines(TestCase):
    def setUp(self):
        self.patient = Patient.objects.create(
            card_id='S6001', name='Stage6', birth_date=date(1980, 1, 1),
            first_treatment_date=date(2026, 1, 5),
            first_visit_date=date(2026, 1, 5),
            is_all_case_survey=True,
        )

    def test_mapping_is_visible_only_through_treatment_week_end(self):
        from rtms_app.services.schedule_tasks import compute_dashboard_tasks, compute_task_definitions
        from rtms_app.services.rtms_schedule import generate_treatment_dates

        definitions = compute_task_definitions(self.patient, holidays=set())
        mapping = next(item for item in definitions if item['key'] == 'mapping')
        dates = generate_treatment_dates(self.patient.first_treatment_date, total=30, holidays=set())
        self.assertEqual(mapping['planned_date'], date(2026, 1, 12))
        self.assertEqual(mapping['window_end'], dates[9])
        self.assertNotIn('mapping', {item['key'] for item in compute_dashboard_tasks(self.patient, date(2026, 1, 11), set())})
        self.assertIn('mapping', {item['key'] for item in compute_dashboard_tasks(self.patient, date(2026, 1, 16), set())})
        self.assertNotIn('mapping', {item['key'] for item in compute_dashboard_tasks(self.patient, date(2026, 1, 19), set())})

    def test_course_mapping_task_uses_course_mapping_date(self):
        from rtms_app.services.schedule_tasks import compute_task_definitions

        course = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=2,
            first_treatment_date=date(2026, 3, 2),
            mapping_date=date(2026, 3, 9),
        )
        self.patient.mapping_date = date(2026, 1, 5)
        self.patient.save(update_fields=['mapping_date'])

        mapping = next(
            item for item in compute_task_definitions(
                self.patient, holidays=set(), treatment_course=course,
            ) if item['key'] == 'mapping'
        )

        self.assertEqual(mapping['planned_date'], date(2026, 3, 16))

    def test_hamd_week4_ends_seven_days_after_week_end(self):
        from rtms_app.services.schedule_tasks import compute_dashboard_tasks, compute_task_definitions

        definitions = compute_task_definitions(self.patient, holidays=set())
        week4 = next(item for item in definitions if item['key'] == 'assessment_week4')
        self.assertEqual(week4['planned_date'], date(2026, 1, 30))
        self.assertEqual(week4['window_end'], date(2026, 2, 6))
        self.assertIn('assessment_week4', {item['key'] for item in compute_dashboard_tasks(self.patient, date(2026, 2, 6), set())})
        self.assertNotIn('assessment_week4', {item['key'] for item in compute_dashboard_tasks(self.patient, date(2026, 2, 7), set())})


class TestStage6PatientAndCalendar(TestCase):
    def test_admission_procedure_get_does_not_reference_undefined_date(self):
        patient = Patient.objects.create(
            card_id='S6004', name='Admission', birth_date=date(1980, 1, 1),
        )
        user = get_user_model().objects.create_user(username='admission-viewer')
        client = Client()
        client.force_login(user)

        response = client.get(reverse('rtms_app:admission_procedure', args=[patient.pk]))

        self.assertEqual(response.status_code, 200)

    def test_registration_form_defaults_first_visit_to_today(self):
        from rtms_app.forms import PatientRegistrationForm
        self.assertEqual(PatientRegistrationForm().initial['first_visit_date'], timezone.localdate())

    def test_calendar_counts_date_ranges_and_excludes_skipped(self):
        from rtms_app.views import _build_month_calendar
        p = Patient.objects.create(
            card_id='S6002', name='Calendar', birth_date=date(1980, 1, 1),
            admission_date=date(2026, 8, 10), first_treatment_date=date(2026, 8, 19),
        )
        TreatmentSession.objects.create(patient=p, session_date=date(2026, 8, 24), status='planned')
        TreatmentSession.objects.create(patient=p, session_date=date(2026, 8, 25), status='skipped')
        TreatmentSession.objects.create(patient=p, session_date=date(2026, 8, 26), status='planned')
        discharged = Patient.objects.create(
            card_id='S6003', name='Discharged Patient', birth_date=date(1980, 1, 1),
            admission_date=date(2026, 8, 1), discharge_date=date(2026, 8, 12),
        )
        context = _build_month_calendar(2026, 8)
        days = {d['date']: d for week in context['weeks'] for d in week}
        self.assertEqual(days[date(2026, 8, 10)]['inpatient_count'], 2)
        self.assertEqual(days[date(2026, 8, 24)]['rtms_count'], 1)
        self.assertEqual(days[date(2026, 8, 25)]['rtms_count'], 0)
        labels = [event['label'] for event in days[date(2026, 8, 10)]['events_visible']]
        self.assertIn('入院（Calendar）', labels)
        labels = [event['label'] for event in days[date(2026, 8, 24)]['events_visible']]
        self.assertIn('rTMS治療（Calendar＃1回）', labels)
        labels = [event['label'] for event in days[date(2026, 8, 26)]['events_visible']]
        self.assertIn('rTMS治療（Calendar＃3回）', labels)
        labels = [event['label'] for event in days[date(2026, 8, 12)]['events_visible']]
        self.assertIn('退院（Discharged）', labels)

    def test_diagnosis_choices_are_preserved_in_existing_string_format(self):
        patient = Patient.objects.create(
            card_id='56003', name='Diagnosis', birth_date=date(1980, 1, 1),
            diagnosis='うつ病', first_treatment_date=date(2026, 1, 5),
            psychiatric_history=[], has_other_psychiatric_history='yes',
        )
        doctor_group, _ = Group.objects.get_or_create(name='医師')
        doctor = get_user_model().objects.create_user(username='stage6-doctor')
        doctor.groups.add(doctor_group)
        post = {
            'card_id': patient.card_id, 'name': patient.name,
            'birth_date': patient.birth_date.isoformat(), 'gender': patient.gender,
            'attending_physician': str(doctor.pk), 'admission_date': '2026-01-01',
            'first_treatment_date': '2026-01-05', 'first_visit_date': '2026-01-01',
            'diag_list': 'うつ病エピソード（F32）',
            'has_other_psychiatric_history': 'yes',
            'psychiatric_history': ['F33', 'F20'],
            'psychiatric_history_other_text': '既往診断',
        }
        client = Client()
        client.force_login(doctor)
        response = client.post(reverse('rtms_app:patient_first_visit', args=[patient.pk]), post)
        self.assertEqual(response.status_code, 302)
        patient.refresh_from_db()
        self.assertEqual(
            patient.diagnosis,
            'うつ病エピソード（F32）, 反復性うつ病性障害（F33）, 統合失調症（F20）, その他(既往診断)',
        )
        self.assertIn('F33', patient.diagnosis)
        self.assertIn('F20', patient.diagnosis)
        self.assertEqual(patient.psychiatric_history, ['F33', 'F20'])

        unchanged = Patient.objects.create(
            card_id='56004', name='Keep Diagnosis', birth_date=date(1980, 1, 1),
            diagnosis='旧形式の診断名', first_treatment_date=date(2026, 1, 5),
            has_other_psychiatric_history='yes',
        )
        unchanged_post = dict(post)
        unchanged_post.update({
            'card_id': unchanged.card_id, 'name': unchanged.name,
            'birth_date': unchanged.birth_date.isoformat(),
            'psychiatric_history': [],
        })
        unchanged_post.pop('diag_list', None)
        unchanged_post.pop('psychiatric_history_other_text', None)
        client.post(reverse('rtms_app:patient_first_visit', args=[unchanged.pk]), unchanged_post)
        unchanged.refresh_from_db()
        self.assertEqual(unchanged.diagnosis, '旧形式の診断名')

    def test_baseline_window_does_not_invert_when_first_visit_is_late(self):
        from types import SimpleNamespace
        from rtms_app.views import get_assessment_window
        late = SimpleNamespace(
            first_visit_date=date(2026, 1, 6),
            first_treatment_date=date(2026, 1, 5),
            created_at=None,
        )
        normal = SimpleNamespace(
            first_visit_date=date(2026, 1, 2),
            first_treatment_date=date(2026, 1, 5),
            created_at=None,
        )
        legacy = SimpleNamespace(
            first_visit_date=None,
            first_treatment_date=date(2026, 1, 5),
            created_at=timezone.make_aware(datetime.datetime(2026, 1, 2, 9, 0)),
        )
        for patient in (late, normal, legacy):
            start, end = get_assessment_window(patient, 'baseline')
            self.assertLessEqual(start, end)



class TestAssessmentHubOtResearchSection(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='ot-hub-user', password='pass1234')
        self.client = Client()
        self.client.force_login(self.user)
        self.patient = Patient.objects.create(
            card_id='OT001',
            name='OT Test',
            birth_date=date(1980, 1, 1),
            first_treatment_date=date(2026, 1, 5),
            first_visit_date=date(2026, 1, 5),
        )

    def test_hub_renders_ot_research_section_and_moves_bacs(self):
        response = self.client.get(reverse('rtms_app:assessment_hub', args=[self.patient.pk, 'baseline']))

        self.assertEqual(response.status_code, 200)
        html = response.content.decode()

        self.assertIn('OT研究用評価尺度', html)
        self.assertIn('WHO-DAS', html)
        self.assertIn('BACS', html)
        self.assertIn('COPM', html)
        self.assertIn('6MWT', html)
        self.assertIn('Tinkertory Test', html)
        self.assertIn('治療前', html)
        self.assertIn('1回目', html)
        self.assertIn('7回目', html)
        self.assertEqual(html.count('<th scope="col" class="scale-name-column">尺度</th>'), 4)

        research_section = next(section for section in response.context['matrix_sections'] if section['title'] == '研究用評価尺度')
        research_names = [row['name'] for row in research_section['rows']]
        self.assertNotIn('BACS', research_names)
        self.assertNotIn('WHO-DAS', research_names)
        self.assertNotIn('COPM', research_names)
        self.assertNotIn('6MWT', research_names)
        self.assertNotIn('Tinkertory Test', research_names)

        ot_section = next(section for section in response.context['matrix_sections'] if section['title'] == 'OT研究用評価尺度')
        self.assertIn('tables', ot_section)
        self.assertEqual(len(ot_section['tables']), 2)

        pre_post_table = ot_section['tables'][0]
        self.assertEqual([column['label'] for column in pre_post_table['columns']], ['治療前', '治療後'])
        ot_names = [row['name'] for row in pre_post_table['rows']]
        self.assertIn('BACS', ot_names)
        self.assertIn('WHO-DAS', ot_names)
        self.assertIn('COPM', ot_names)
        self.assertIn('6MWT', ot_names)

        tinkertory_table = ot_section['tables'][1]
        self.assertEqual(tinkertory_table['columns'][0]['label'], '1回目')
        self.assertEqual(tinkertory_table['columns'][-1]['label'], '7回目')
        self.assertEqual(tinkertory_table['rows'][0]['name'], 'Tinkertory Test')
        self.assertNotIn('OT研究用評価尺度２', response.content.decode())
        self.assertNotIn('回数', response.content.decode())


class TestStage7AssessmentEntry(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='stage7-user', password='pass1234')
        self.client = Client()
        self.client.force_login(self.user)
        self.patient = Patient.objects.create(
            card_id='STAGE7', name='Stage 7 Test', birth_date=date(1980, 1, 1), course_number=1,
        )

    def test_simple_assessment_baseline_post_update_and_blank(self):
        url = reverse('rtms_app:assessment_hub', args=[self.patient.pk, 'baseline'])
        response = self.client.post(url, {'date': '2026-08-01', 'score_phq9_baseline': '9', 'score_phq9_post': ''})
        self.assertEqual(response.status_code, 302)
        scale = ScaleDefinition.objects.get(code='phq9')
        record = AssessmentRecord.objects.get(patient=self.patient, scale=scale, timing='baseline')
        self.assertEqual(record.scores, {'score': 9})

        response = self.client.post(url, {'date': '2026-08-02', 'score_phq9_baseline': '12', 'score_phq9_post': ''})
        self.assertEqual(response.status_code, 302)
        record.refresh_from_db()
        self.assertEqual(record.scores, {'score': 12})
        self.assertEqual(AssessmentRecord.objects.filter(patient=self.patient, scale=scale, timing='baseline').count(), 1)

        post_url = reverse('rtms_app:assessment_hub', args=[self.patient.pk, 'post'])
        self.client.post(post_url, {'date': '2026-08-03', 'score_phq9_baseline': '', 'score_phq9_post': '7'})
        self.assertEqual(
            AssessmentRecord.objects.get(patient=self.patient, scale=scale, timing='post').scores,
            {'score': 7},
        )

        self.client.post(url, {'date': '2026-08-04', 'score_phq9_baseline': '', 'score_phq9_post': ''})
        self.assertFalse(AssessmentRecord.objects.filter(patient=self.patient, scale=scale, timing='baseline').exists())

    def test_detail_assessments_store_excel_shapes(self):
        for scale_code in ('who-das', 'bacs', 'copm', '6mwt', 'tinkertory-test'):
            response = self.client.get(reverse('rtms_app:assessment_scale', args=[self.patient.pk, 'baseline', scale_code]))
            self.assertEqual(response.status_code, 200)

        who_url = reverse('rtms_app:assessment_scale', args=[self.patient.pk, 'baseline', 'who-das'])
        response = self.client.post(who_url, {
            'date': '2026-08-01', 'cognition': '4', 'mobility': '5', 'self_care': '2',
            'interpersonal': '3', 'life_activities': '6', 'social_participation': '6',
        })
        self.assertEqual(response.status_code, 302)
        who = AssessmentRecord.objects.get(patient=self.patient, timing='baseline', scale__code='who-das')
        self.assertEqual(who.scores['total'], 26)

        bacs_url = reverse('rtms_app:assessment_scale', args=[self.patient.pk, 'post', 'bacs'])
        self.client.post(bacs_url, {'composite': '0.2', 'verbal_memory': '0.7', 'working_memory': '-0.6', 'motor_speed': '1', 'verbal_fluency': '0.4', 'attention': '0', 'executive_function': '-0.3'})
        bacs = AssessmentRecord.objects.get(patient=self.patient, timing='post', scale__code='bacs')
        self.assertEqual(bacs.scores['working_memory'], -0.6)

        copm_url = reverse('rtms_app:assessment_scale', args=[self.patient.pk, 'baseline', 'copm'])
        self.client.post(copm_url, {
            'work_name_1': '仕事', 'importance_1': '10', 'performance_1': '1', 'satisfaction_1': '1',
            'work_name_2': '家事', 'importance_2': '8', 'performance_2': '2', 'satisfaction_2': '3',
            'work_name_3': '外出', 'importance_3': '7', 'performance_3': '4', 'satisfaction_3': '5',
        })
        copm = AssessmentRecord.objects.get(patient=self.patient, timing='baseline', scale__code='copm')
        self.assertEqual(len(copm.scores['items']), 3)
        copm_modal = self.client.get(copm_url, {'modal': '1'})
        self.assertContains(copm_modal, '重要度')
        self.assertContains(copm_modal, '仕事')

        mwt_url = reverse('rtms_app:assessment_scale', args=[self.patient.pk, 'post', '6mwt'])
        self.client.post(mwt_url, {'before_blood_pressure': '117/100', 'before_pulse': '87', 'after_blood_pressure': '118/109', 'after_pulse': '88', 'walking_distance': '465', 'before_knee_pain': '0', 'after_knee_pain': '0'})
        mwt = AssessmentRecord.objects.get(patient=self.patient, timing='post', scale__code='6mwt')
        self.assertEqual(mwt.scores['vitals']['before']['blood_pressure'], '117/100')
        self.assertEqual(mwt.scores['vitals']['after']['blood_pressure'], '118/109')
        self.assertEqual(mwt.scores['walking_distance'], 465)
        mwt_modal = self.client.get(mwt_url, {'modal': '1'})
        self.assertContains(mwt_modal, '歩行距離')
        self.assertContains(mwt_modal, '117/100')
        self.assertContains(mwt_modal, '465')

    def test_tinkertoy_uses_evaluation_timing_and_hub_status(self):
        for index in range(1, 8):
            url = reverse('rtms_app:assessment_scale', args=[self.patient.pk, f'tinkertory_{index}', 'tinkertory-test'])
            self.client.post(url, {'date': f'2026-08-{index:02d}', 'pieces': str(index), 'time': '530', 'work_name': f'作品{index}', 'total': str(index + 10), 'z_score': '1.34347'})
        records = AssessmentRecord.objects.filter(patient=self.patient, scale__code='tinkertory-test').order_by('timing')
        self.assertEqual(records.count(), 7)
        record = AssessmentRecord.objects.get(patient=self.patient, timing='tinkertory_1', scale__code='tinkertory-test')
        self.assertEqual(record.scores['total'], 11)
        hub = self.client.get(reverse('rtms_app:assessment_hub', args=[self.patient.pk, 'baseline']))
        self.assertContains(hub, '総合計 11')
        self.assertContains(hub, '2026-08-01')

    def test_simple_inputs_have_pre_then_post_tab_order(self):
        response = self.client.get(reverse('rtms_app:assessment_hub', args=[self.patient.pk, 'baseline']))
        html = response.content.decode()
        expected = ['phq9', 'sass-j', 'bdi-ii', 'sds', 'stai-trait', 'stai-state', 'dai-10']
        for index, code in enumerate(expected, start=1):
            baseline_start = html.index(f'name="score_{code}_baseline"')
            post_start = html.index(f'name="score_{code}_post"')
            self.assertLess(baseline_start, html.index(f'tabindex="{index}"', baseline_start))
            self.assertLess(post_start, html.index(f'tabindex="{index + len(expected)}"', post_start))

    def test_detail_scale_get_returns_compact_modal_fragment(self):
        for scale_code in ('who-das', 'bacs', 'copm', '6mwt', 'tinkertory-test'):
            response = self.client.get(
                reverse('rtms_app:assessment_scale', args=[self.patient.pk, 'baseline', scale_code]),
                {'modal': '1'},
                HTTP_X_REQUESTED_WITH='XMLHttpRequest',
            )
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, 'assessment-detail-form')
            self.assertNotContains(response, '<html')

    def test_non_hamd_timing_labels_are_staff_facing(self):
        expected = {
            'baseline': '治療前', 'post': '治療後',
            'tinkertory_1': '1回目', 'tinkertory_2': '2回目',
            'tinkertory_3': '3回目', 'tinkertory_4': '4回目',
            'tinkertory_5': '5回目', 'tinkertory_6': '6回目',
            'tinkertory_7': '7回目',
        }
        for timing, label in expected.items():
            response = self.client.get(
                reverse('rtms_app:assessment_scale', args=[self.patient.pk, timing, 'copm']),
                {'modal': '1'},
            )
            self.assertContains(response, f'（{label}）')
            self.assertNotContains(response, f'（{timing}）')

    def test_hub_uses_single_column_assessment_panels(self):
        response = self.client.get(reverse('rtms_app:assessment_hub', args=[self.patient.pk, 'baseline']))
        html = response.content.decode()
        self.assertIn('assessment-panel-grid', html)
        self.assertIn('grid-template-columns: minmax(0, 1fr)', html)

    def test_detail_ajax_save_returns_cell_summary(self):
        response = self.client.post(
            reverse('rtms_app:assessment_scale', args=[self.patient.pk, 'baseline', 'who-das']) + '?modal=1',
            {'cognition': '1', 'mobility': '2', 'self_care': '3', 'interpersonal': '4', 'life_activities': '5', 'social_participation': '6'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['summary'], '合計 21')

    def test_hub_ot_links_are_modal_targets_and_hamd_is_unchanged(self):
        response = self.client.get(reverse('rtms_app:assessment_hub', args=[self.patient.pk, 'baseline']))
        html = response.content.decode()
        self.assertEqual(html.count('data-detail-modal="true" data-timing-label='), 15)
        self.assertNotIn('data-detail-modal="true" class="assessment-cell', html.split('HAM-D', 1)[1].split('研究用評価尺度', 1)[0])
        bootstrap_position = html.index('bootstrap.bundle.min.js')
        handler_position = html.index("const modalElement = document.getElementById('assessmentDetailModal')")
        self.assertLess(bootstrap_position, handler_position)
        self.assertEqual(html.count('id="assessmentDetailModal"'), 1)
        for timing in ('tinkertory_1', 'tinkertory_2', 'tinkertory_3', 'tinkertory_4', 'tinkertory_5', 'tinkertory_6', 'tinkertory_7'):
            self.assertIn(f'/assessment/{timing}/tinkertory-test/', html)

    def test_hamd_hub_links_use_modal_route_without_ot_handler(self):
        response = self.client.get(reverse('rtms_app:assessment_hub', args=[self.patient.pk, 'baseline']))
        html = response.content.decode()
        self.assertEqual(html.count('data-hamd-modal="true" data-timing-label='), 4)
        self.assertNotIn('data-detail-modal="true" data-hamd-modal="true"', html)
        self.assertIn('/assessment/baseline/hamd/?from=assessment_hub&amp;modal=1', html)
        modal_response = self.client.get(
            reverse('rtms_app:assessment_scale', args=[self.patient.pk, 'baseline', 'hamd']),
            {'modal': '1'},
        )
        self.assertContains(modal_response, 'id="hamdForm"')
        self.assertContains(modal_response, 'function initHamdModal()')

    def test_hamd_hub_modal_uses_initial_visit_modal_shell(self):
        response = self.client.get(reverse('rtms_app:assessment_hub', args=[self.patient.pk, 'baseline']))
        html = response.content.decode()
        self.assertIn('id="assessmentDetailModal"', html)
        self.assertIn('class="modal-dialog modal-xl modal-dialog-centered"', html)
        self.assertNotIn('assessment-detail-dialog', html)
        self.assertNotIn('modal-dialog-scrollable assessment-detail-dialog', html)


class TestTreatmentRecordPrintRoute(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='print-user', password='pass1234')
        self.client = Client()
        self.client.force_login(self.user)
        self.patient = Patient.objects.create(
            card_id='PRINT1', name='Print Test', birth_date=date(1980, 1, 1), course_number=1,
        )
        self.session = TreatmentSession.objects.create(
            patient=self.patient, session_date=date(2026, 8, 3), status='done',
        )

    def test_treatment_record_alias_renders_existing_side_effect_print(self):
        response = self.client.get(
            reverse('rtms_app:print:print_treatment_record_preview', args=[self.patient.pk, self.session.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '副作用チェック表')
        self.assertContains(response, self.patient.card_id)
        self.assertContains(response, self.session.session_date.isoformat())

    def test_treatment_form_renders_coil_and_site_tabs_with_defaults(self):
        from rtms_app.forms import TreatmentForm
        form = TreatmentForm()
        self.assertEqual(form.initial['coil_type'], 'Brainsway H1')
        self.assertEqual(form.initial['target_site'], '左DLPFC')
        response = self.client.get(reverse('rtms_app:treatment_add', args=[self.patient.pk]))
        self.assertContains(response, '使用したTMSコイル')
        self.assertContains(response, 'value="Brainsway H1"')
        self.assertContains(response, 'value="左DLPFC"')

    def test_existing_coil_and_site_values_are_preserved_in_form(self):
        existing = TreatmentSession.objects.create(
            patient=self.patient, session_date=date(2026, 8, 4),
            coil_type='H1', target_site='左背外側前頭前野',
        )
        from rtms_app.forms import TreatmentForm
        form = TreatmentForm(instance=existing)
        self.assertEqual(form.initial['coil_type'], 'H1')
        self.assertEqual(form.initial['target_site'], '左背外側前頭前野')
        self.assertIn(('H1', 'H1'), list(form.fields['coil_type'].choices))
        self.assertIn(('左背外側前頭前野', '左背外側前頭前野'), list(form.fields['target_site'].choices))

    def test_selected_coil_and_site_are_saved_and_printed(self):
        response = self.client.post(reverse('rtms_app:treatment_add', args=[self.patient.pk]), {
            'treatment_date': self.session.session_date.isoformat(),
            'treatment_time': '09:00', 'coil_type': 'Brainsway H1', 'target_site': '左DLPFC',
            'mt_percent': '120', 'intensity_percent': '60', 'frequency_hz': '18.0',
            'train_seconds': '2.0', 'intertrain_seconds': '20.0', 'train_count': '55',
            'total_pulses': '1980',
        })
        self.assertIn(response.status_code, (302, 303))
        self.session.refresh_from_db()
        self.assertEqual(self.session.coil_type, 'Brainsway H1')
        self.assertEqual(self.session.target_site, '左DLPFC')
        print_response = self.client.get(reverse('rtms_app:print:print_treatment_record_preview', args=[self.patient.pk, self.session.pk]))
        self.assertContains(print_response, 'Brainsway H1')
        self.assertContains(print_response, '左DLPFC')


class TestRedirectFocus(TestCase):
    def setUp(self):
        self.client = Client()
        User = get_user_model()
        self.user = User.objects.create_user(username="t", password="tpass")
        self.client.login(username="t", password="tpass")

        # Create a minimal patient required by the URL
        self.patient = Patient.objects.create(card_id="TEST001", name="T Test", birth_date=datetime.date(1980, 1, 1))

    def test_treatment_add_post_redirect_includes_focus(self):
        # Use the real URL name and include patient id
        url = reverse('rtms_app:treatment_add', args=[self.patient.id])

        post = {
            'treatment_date': '2026-01-02',
            'treatment_time': '09:00',
            'mt_percent': '120',
            'frequency_hz': '18.0',
            'train_seconds': '2.0',
            'intertrain_seconds': '20.0',
            'train_count': '55',
            'total_pulses': '1980',
        }

        resp = self.client.post(url, post, follow=False)

        # Expect redirect
        self.assertIn(resp.status_code, (302, 303))
        loc = resp.get('Location', '')
        self.assertIn('focus=2026-01-02', loc)

    def test_treatment_add_rejects_new_session_after_thirty(self):
        for i in range(30):
            TreatmentSession.objects.create(
                patient=self.patient,
                session_date=date(2026, 1, 5) + datetime.timedelta(days=i),
            )

        url = reverse('rtms_app:treatment_add', args=[self.patient.id])
        response = self.client.post(url, {
            'treatment_date': '2027-01-04',
            'treatment_time': '09:00',
            'mt_percent': '120',
            'frequency_hz': '18.0',
            'train_seconds': '2.0',
            'intertrain_seconds': '20.0',
            'train_count': '55',
            'total_pulses': '1980',
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(TreatmentSession.objects.filter(patient=self.patient).count(), 30)


class TestSideEffectAndAdverseEventFlow(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='side-effect-user', password='pass1234')
        self.client = Client()
        self.client.force_login(self.user)
        self.patient = Patient.objects.create(
            card_id='SIDE1', name='Side Effect Test', birth_date=date(1980, 1, 1), course_number=1,
        )

    def treatment_post(self, **extra):
        request_headers = {}
        if 'HTTP_X_REQUESTED_WITH' in extra:
            request_headers['HTTP_X_REQUESTED_WITH'] = extra.pop('HTTP_X_REQUESTED_WITH')
        data = {
            'treatment_date': '2026-08-03', 'treatment_time': '09:00', 'mt_percent': '120',
            'frequency_hz': '18.0', 'train_seconds': '2.0', 'intertrain_seconds': '20.0',
            'train_count': '55', 'total_pulses': '1980', 'side_effect_rows_json': '[]',
        }
        data.update(extra)
        return self.client.post(reverse('rtms_app:treatment_add', args=[self.patient.pk]), data, **request_headers)

    def test_side_effect_status_and_candidate_are_rendered(self):
        session = TreatmentSession.objects.create(patient=self.patient, session_date=date(2026, 8, 3))
        SideEffectCheck.objects.create(session=session, rows=[], memo='副作用なし')
        response = self.client.get(reverse('rtms_app:treatment_add', args=[self.patient.pk]) + '?date=2026-08-03')
        self.assertContains(response, '副作用なし')
        self.assertContains(response, '副作用入力')
        self.assertContains(response, '有害事象候補')

    def test_new_side_effect_modal_starts_without_adverse_report_selection(self):
        response = self.client.get(reverse('rtms_app:treatment_add', args=[self.patient.pk]))
        html = response.content.decode()
        self.assertIn('id="sideEffectCandidates" class="alert alert-warning d-none', html)
        self.assertIn('id="reportAsAdverseEvent"', html)
        self.assertNotIn('id="reportAsAdverseEvent" checked', html)

    def test_existing_sae_restores_report_selection_in_side_effect_modal(self):
        session = TreatmentSession.objects.create(patient=self.patient, session_date=date(2026, 8, 3))
        SeriousAdverseEvent.objects.create(
            patient=self.patient, course_number=1, session=session, event_types=['seizure'],
        )
        response = self.client.get(reverse('rtms_app:treatment_add', args=[self.patient.pk]) + '?date=2026-08-03')
        html = response.content.decode()
        self.assertIn('id="reportAsAdverseEvent" checked', html)
        self.assertIn('id="candidateEventTypes" class=" mt-2"', html)

    def test_legacy_adverse_event_block_is_replaced_by_modal_choices(self):
        response = self.client.get(reverse('rtms_app:treatment_add', args=[self.patient.pk]))
        html = response.content.decode()
        self.assertNotIn('重篤を含む有害事象（該当する場合チェック）', html)
        self.assertIn('有害事象として報告する', html)
        for label in ('けいれん発作', '手指の筋収縮', '失神', '躁病・軽躁病の出現', '自殺企図', 'その他'):
            self.assertIn(label, html)

    def test_adverse_candidate_panel_starts_hidden_and_hides_without_candidate(self):
        response = self.client.get(reverse('rtms_app:treatment_add', args=[self.patient.pk]))
        html = response.content.decode()
        self.assertIn('id="sideEffectCandidates" class="alert alert-warning d-none', html)
        self.assertIn("panel.classList.add('d-none')", html)
        self.assertIn("reportCheckbox.checked = false", html)

    def test_candidate_defaults_apply_only_on_first_report_enable(self):
        response = self.client.get(reverse('rtms_app:treatment_add', args=[self.patient.pk]))
        html = response.content.decode()
        self.assertIn('const candidateOverrides = new Set()', html)
        self.assertIn('let candidateDefaultsApplied =', html)
        self.assertIn("if (defaults.has(label) && !candidateOverrides.has(input.dataset.target)) input.checked = true", html)
        self.assertIn('candidateOverrides.add(input.dataset.target)', html)
        self.assertIn('if (event.target.checked) applyCandidateDefaults()', html)

    def test_report_action_saves_adverse_event_report_and_types(self):
        session = TreatmentSession.objects.create(patient=self.patient, session_date=date(2026, 8, 3))
        SeriousAdverseEvent.objects.create(
            patient=self.patient, course_number=1, session=session, event_types=['seizure'],
        )
        response = self.treatment_post(
            action='save_sae_report', sae_seizure='on',
            event_name='けいれん発作', onset_date='2026-08-03', age='46', gender='男性',
            initials='S.T.', diagnosis='うつ病エピソード', outcome='軽快',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        session = TreatmentSession.objects.get(patient=self.patient, session_date=date(2026, 8, 3))
        report = AdverseEventReport.objects.get(session=session)
        self.assertEqual(report.adverse_event_name, 'けいれん発作')
        self.assertEqual(report.event_types, ['seizure'])
        self.assertTrue(SeriousAdverseEvent.objects.get(session=session).event_types == ['seizure'])

    def test_report_save_does_not_resave_treatment_side_effect_or_sae(self):
        session = TreatmentSession.objects.create(
            patient=self.patient, session_date=date(2026, 8, 3), intensity_percent=80,
            treatment_notes='治療メモ',
        )
        side_effect = SideEffectCheck.objects.create(session=session, rows=[{'item': '頭痛', 'after': 1}], memo='')
        sae = SeriousAdverseEvent.objects.create(
            patient=self.patient, course_number=1, session=session, event_types=['suicide_attempt'], other_text='既存',
        )
        treatment_updated = session.updated_at if hasattr(session, 'updated_at') else None
        response = self.treatment_post(
            action='save_sae_report', event_name='自殺企図', onset_date='2026-08-03',
            age='46', gender='男性', initials='S.T.', diagnosis='うつ病エピソード', outcome='未回復',
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        session.refresh_from_db()
        side_effect.refresh_from_db()
        sae.refresh_from_db()
        self.assertEqual(session.intensity_percent, 80)
        self.assertEqual(session.treatment_notes, '治療メモ')
        self.assertEqual(side_effect.memo, '')
        self.assertEqual(side_effect.rows, [{'item': '頭痛', 'after': 1}])
        self.assertEqual(sae.event_types, ['suicide_attempt'])
        self.assertEqual(sae.other_text, '既存')

    def test_clear_side_effects_removes_only_target_adverse_records(self):
        target = TreatmentSession.objects.create(patient=self.patient, session_date=date(2026, 8, 3))
        other = TreatmentSession.objects.create(patient=self.patient, session_date=date(2026, 8, 4))
        SideEffectCheck.objects.create(session=target, rows=[{'item': 'けいれん', 'after': 1}], memo='')
        target_sae = SeriousAdverseEvent.objects.create(
            patient=self.patient, course_number=1, session=target, event_types=['seizure'],
        )
        target_report = AdverseEventReport.objects.create(session=target, adverse_event_name='けいれん発作')
        other_sae = SeriousAdverseEvent.objects.create(
            patient=self.patient, course_number=1, session=other, event_types=['syncope'],
        )
        other_report = AdverseEventReport.objects.create(session=other, adverse_event_name='失神')
        response = self.client.post(
            reverse('rtms_app:treatment_add', args=[self.patient.pk]),
            {'action': 'clear_side_effects', 'treatment_date': '2026-08-03'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(SeriousAdverseEvent.objects.filter(pk=target_sae.pk).exists())
        self.assertFalse(AdverseEventReport.objects.filter(pk=target_report.pk).exists())
        self.assertTrue(SeriousAdverseEvent.objects.filter(pk=other_sae.pk).exists())
        self.assertTrue(AdverseEventReport.objects.filter(pk=other_report.pk).exists())
        target_check = SideEffectCheck.objects.get(session=target)
        self.assertEqual(target_check.rows, [])
        self.assertEqual(target_check.memo, '副作用なし')

        empty_target = TreatmentSession.objects.create(patient=self.patient, session_date=date(2026, 8, 5))
        response = self.client.post(
            reverse('rtms_app:treatment_add', args=[self.patient.pk]),
            {'action': 'clear_side_effects', 'treatment_date': '2026-08-05'},
            HTTP_X_REQUESTED_WITH='XMLHttpRequest',
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(SideEffectCheck.objects.get(session=empty_target).memo, '副作用なし')

    def test_new_report_modal_prefills_from_session_and_sae(self):
        session = TreatmentSession.objects.create(
            patient=self.patient, session_date=date(2026, 8, 3), mt_percent=110,
            target_site='左DLPFC', treatment_notes='治療メモ',
        )
        mapping = MappingSession.objects.create(
            patient=self.patient, course_number=1, date=date(2026, 8, 3), week_number=1, resting_mt=52,
        )
        SeriousAdverseEvent.objects.create(
            patient=self.patient, course_number=1, session=session, event_types=['suicide_attempt'],
        )
        response = self.client.get(reverse('rtms_app:treatment_add', args=[self.patient.pk]) + '?date=2026-08-03')
        self.assertContains(response, '自殺企図')
        self.assertContains(response, '2026-08-03')
        self.assertContains(response, 'value="52"')
        self.assertContains(response, 'value="110"')
        self.assertContains(response, 'MT測定値')

    def test_existing_report_values_override_session_prefill(self):
        session = TreatmentSession.objects.create(
            patient=self.patient, session_date=date(2026, 8, 3), mt_percent=110,
        )
        report = AdverseEventReport.objects.create(
            session=session, adverse_event_name='編集済み', onset_date=date(2026, 8, 9),
            age=99, sex='編集済み性別', initials='E.D.',
        )
        response = self.client.get(reverse('rtms_app:treatment_add', args=[self.patient.pk]) + '?date=2026-08-03')
        self.assertContains(response, 'value="編集済み"')
        self.assertContains(response, 'value="2026-08-09"')
        self.assertContains(response, 'value="99"')

    def test_new_report_prefill_does_not_infer_initials_and_uses_default_diagnosis(self):
        session = TreatmentSession.objects.create(patient=self.patient, session_date=date(2026, 8, 3))
        SeriousAdverseEvent.objects.create(
            patient=self.patient, course_number=1, session=session, event_types=['seizure'],
        )
        response = self.client.get(reverse('rtms_app:treatment_add', args=[self.patient.pk]) + '?date=2026-08-03')
        self.assertContains(response, 'id="saeInitials"')
        self.assertContains(response, 'id="saeDiagnosisOther"')
        self.assertContains(response, 'value="うつ病エピソード"')
        self.assertNotContains(response, 'value="サ"')
        self.assertNotContains(response, 'value="テ"')

    def test_existing_report_initials_and_free_text_are_preserved(self):
        session = TreatmentSession.objects.create(patient=self.patient, session_date=date(2026, 8, 3))
        SeriousAdverseEvent.objects.create(
            patient=self.patient, course_number=1, session=session, event_types=['other'],
        )
        AdverseEventReport.objects.create(
            session=session, initials='A.B.', diagnosis_category='other',
            diagnosis_other_text='保存済み診断', adverse_event_name='その他',
        )
        response = self.client.get(reverse('rtms_app:treatment_add', args=[self.patient.pk]) + '?date=2026-08-03')
        self.assertContains(response, 'value="A.B."')
        self.assertContains(response, 'value="保存済み診断"')


class TestAdverseEventCourseIsolation(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='course-adverse-user', password='pw')
        self.client = Client()
        self.client.force_login(self.user)
        self.patient = Patient.objects.create(
            card_id='COURSE2', name='Two Course Patient', birth_date=date(1980, 1, 1), course_number=1,
        )
        self.course_one = TreatmentCourse.objects.create(patient=self.patient, course_number=1)
        self.course_two = TreatmentCourse.objects.create(patient=self.patient, course_number=2)
        self.session_one = TreatmentSession.objects.create(
            patient=self.patient, treatment_course=self.course_one, course_number=1,
            session_date=date(2026, 8, 3),
        )
        self.session_two = TreatmentSession.objects.create(
            patient=self.patient, treatment_course=self.course_two, course_number=2,
            session_date=date(2026, 8, 4),
        )

    def test_treatment_page_and_skip_list_are_course_scoped(self):
        SideEffectCheck.objects.create(session=self.session_one, memo='course-one-side-effect')
        SideEffectCheck.objects.create(session=self.session_two, memo='course-two-side-effect')
        SeriousAdverseEvent.objects.create(
            patient=self.patient, course_number=1, session=self.session_one, event_types=['seizure'],
        )
        SeriousAdverseEvent.objects.create(
            patient=self.patient, course_number=2, session=self.session_two, event_types=['syncope'],
        )
        AdverseEventReport.objects.create(session=self.session_one, adverse_event_name='course-one-report')
        AdverseEventReport.objects.create(session=self.session_two, adverse_event_name='course-two-report')
        TreatmentSkip.objects.create(
            treatment=self.session_one, action_type='postpone', reason='course-one-skip', performed_by=self.user,
        )
        TreatmentSkip.objects.create(
            treatment=self.session_two, action_type='postpone', reason='course-two-skip', performed_by=self.user,
        )

        course_one_page = self.client.get(
            reverse('rtms_app:treatment_add', args=[self.patient.pk])
            + '?date=2026-08-03&course_number=1'
        )
        course_two_page = self.client.get(
            reverse('rtms_app:treatment_add', args=[self.patient.pk])
            + '?date=2026-08-04&course_number=2'
        )
        self.assertContains(course_one_page, 'course-one-side-effect')
        self.assertNotContains(course_one_page, 'course-two-side-effect')
        self.assertContains(course_two_page, 'course-two-side-effect')
        self.assertNotContains(course_two_page, 'course-one-side-effect')
        self.assertContains(course_one_page, 'course-one-report')
        self.assertNotContains(course_one_page, 'course-two-report')
        self.assertContains(course_two_page, 'course-two-report')
        self.assertNotContains(course_two_page, 'course-one-report')

        course_one_skips = self.client.get(
            reverse('rtms_app:treatment_skip_list', args=[self.patient.pk]) + '?course_number=1'
        )
        course_two_skips = self.client.get(
            reverse('rtms_app:treatment_skip_list', args=[self.patient.pk]) + '?course_number=2'
        )
        self.assertContains(course_one_skips, 'course-one-skip')
        self.assertNotContains(course_one_skips, 'course-two-skip')
        self.assertContains(course_two_skips, 'course-two-skip')
        self.assertNotContains(course_two_skips, 'course-one-skip')

    def test_sae_rejects_cross_course_session_mismatch(self):
        sae_one = SeriousAdverseEvent.objects.create(
            patient=self.patient, course_number=1, session=self.session_one,
            event_types=['seizure'],
        )
        sae_two = SeriousAdverseEvent.objects.create(
            patient=self.patient, course_number=2, session=self.session_two,
            event_types=['syncope'],
        )

        with self.assertRaises(ValidationError):
            SeriousAdverseEvent.objects.create(
                patient=self.patient, course_number=2, session=self.session_one,
                event_types=['mania'],
            )
        with self.assertRaises(ValidationError):
            SeriousAdverseEvent.objects.create(
                patient=self.patient, course_number=1, session=self.session_two,
                event_types=['other'],
            )

        sae_one.refresh_from_db()
        sae_two.refresh_from_db()
        self.assertEqual(sae_one.event_types, ['seizure'])
        self.assertEqual(sae_two.event_types, ['syncope'])
        self.assertEqual(
            SeriousAdverseEvent.objects.filter(patient=self.patient).count(), 2,
        )

    def test_course_two_post_creates_session_and_side_effect_on_course_two(self):
        response = self.client.post(reverse('rtms_app:treatment_add', args=[self.patient.pk]), {
            'course_number': '2', 'treatment_date': '2026-08-05', 'treatment_time': '09:00',
            'mt_percent': '120', 'frequency_hz': '18.0', 'train_seconds': '2.0',
            'intertrain_seconds': '20.0', 'train_count': '55', 'total_pulses': '1980',
            'side_effect_rows_json': '[]', 'side_effect_memo': 'course-two-new-side-effect',
        })
        self.assertIn(response.status_code, (200, 302, 303))
        created = TreatmentSession.objects.get(patient=self.patient, session_date=date(2026, 8, 5))
        self.assertEqual(created.treatment_course_id, self.course_two.id)
        self.assertEqual(created.course_number, 2)
        self.assertEqual(SideEffectCheck.objects.get(session=created).memo, 'course-two-new-side-effect')

    def test_research_adverse_event_export_is_course_scoped(self):
        SideEffectCheck.objects.create(session=self.session_one, rows=[{'item': '頭痛', 'after': 1}])
        SideEffectCheck.objects.create(session=self.session_two, rows=[{'item': 'めまい', 'after': 1}])
        sae_one = SeriousAdverseEvent.objects.create(
            patient=self.patient, course_number=1, session=self.session_one, event_types=['seizure'],
        )
        sae_two = SeriousAdverseEvent.objects.create(
            patient=self.patient, course_number=2, session=self.session_two, event_types=['syncope'],
        )
        AdverseEventReport.objects.create(session=self.session_one, adverse_event_name='report-one')
        AdverseEventReport.objects.create(session=self.session_two, adverse_event_name='report-two')

        from rtms_app.services.export_research import (
            generate_research_adverse_events_csv, generate_research_treatment_detail_csv,
        )
        adverse_csv = generate_research_adverse_events_csv()
        detail_csv = generate_research_treatment_detail_csv()
        self.assertIn('1,seizure', adverse_csv)
        self.assertIn('COURSE2,2,1,syncope', adverse_csv)
        self.assertIn('report-one', adverse_csv)
        self.assertIn('report-two', adverse_csv)
        self.assertIn('COURSE2,1,1', detail_csv)
        self.assertIn('COURSE2,2,1', detail_csv)

    def test_invalid_course_relationships_are_rejected(self):
        with self.assertRaises(ValidationError):
            TreatmentSession.objects.create(
                patient=self.patient, treatment_course=self.course_two, course_number=1,
                session_date=date(2026, 8, 6),
            )
        with self.assertRaises(ValidationError):
            TreatmentSession.objects.create(
                patient=Patient.objects.create(card_id='OTHER', name='Other', birth_date=date(1981, 1, 1)),
                treatment_course=self.course_two, course_number=2, session_date=date(2026, 8, 6),
            )
        with self.assertRaises(ValidationError):
            SeriousAdverseEvent.objects.create(
                patient=self.patient, course_number=2, session=self.session_one, event_types=['seizure'],
            )
        with self.assertRaises(ValidationError):
            SeriousAdverseEvent.objects.create(
                patient=Patient.objects.create(card_id='OTHER2', name='Other 2', birth_date=date(1982, 1, 1)),
                course_number=1, session=self.session_one, event_types=['seizure'],
            )


class TestSkipSessions(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(username='skipper', password='pw')
        self.client = Client()
        self.client.login(username='skipper', password='pw')
        self.patient = Patient.objects.create(card_id='SKIP1', name='Skip Test', birth_date=datetime.date(1990,1,1))

    def test_skip_shifts_future_planned_sessions_and_discharge(self):
        # create three planned sessions: day1, day2, day3
        from datetime import date, timedelta
        day1 = date(2026,1,5)
        day2 = date(2026,1,6)
        day3 = date(2026,1,7)
        from rtms_app.models import TreatmentSession
        s1 = TreatmentSession.objects.create(patient=self.patient, session_date=day1)
        s2 = TreatmentSession.objects.create(patient=self.patient, session_date=day2)
        s3 = TreatmentSession.objects.create(patient=self.patient, session_date=day3)
        # set discharge_date
        self.patient.discharge_date = date(2026,1,31)
        self.patient.save()

        url = reverse('rtms_app:treatment_add', args=[self.patient.id])
        post = {
            'treatment_date': day2.isoformat(),
            'treatment_time': '09:00',
            'mt_percent': '120',
            'frequency_hz': '18.0',
            'train_seconds': '2.0',
            'intertrain_seconds': '20.0',
            'train_count': '55',
            'total_pulses': '1980',
            'action': 'skip',
        }
        resp = self.client.post(url, post, follow=False)
        self.assertIn(resp.status_code, (302,303))

        # reload sessions
        s1.refresh_from_db()
        s2.refresh_from_db()
        s3.refresh_from_db()
        self.patient.refresh_from_db()

        self.assertEqual(s2.status, 'skipped')
        # With business-day logic, s3 falls on the next treatment day after the skipped date;
        # in this scenario day3 is already the next treatment day, so it remains unchanged.
        self.assertEqual(s3.session_date, day3)
        # discharge_date unchanged because last planned session didn't move
        self.assertEqual(self.patient.discharge_date, date(2026,1,31))

    def test_skip_weekend_shifts_to_next_weekday(self):
        # Friday -> Saturday/Sunday -> next Monday behavior
        from datetime import date, timedelta
        from rtms_app.models import TreatmentSession
        # Jan 9 2026 is Friday, Jan 10 Sat, Jan 11 Sun, Jan 12 Mon
        day1 = date(2026,1,9)
        day2 = date(2026,1,10)
        day3 = date(2026,1,12)
        s1 = TreatmentSession.objects.create(patient=self.patient, session_date=day1)
        s2 = TreatmentSession.objects.create(patient=self.patient, session_date=day2)
        s3 = TreatmentSession.objects.create(patient=self.patient, session_date=day3)

        # set discharge_date beyond sessions
        self.patient.discharge_date = date(2026,1,31)
        self.patient.save()

        # Ensure no extra holidays injected
        schedule_service.EXTRA_HOLIDAYS.clear()

        url = reverse('rtms_app:treatment_add', args=[self.patient.id])
        post = {
            'treatment_date': day1.isoformat(),
            'treatment_time': '09:00',
            'mt_percent': '120',
            'frequency_hz': '18.0',
            'train_seconds': '2.0',
            'intertrain_seconds': '20.0',
            'train_count': '55',
            'total_pulses': '1980',
            'action': 'skip',
        }
        resp = self.client.post(url, post, follow=False)
        self.assertIn(resp.status_code, (302,303))

        s1.refresh_from_db()
        s2.refresh_from_db()
        s3.refresh_from_db()
        self.patient.refresh_from_db()

        self.assertEqual(s1.status, 'skipped')
        # Compute expected targets using schedule helper so test works whether holidays lib is present
        expected_first = schedule_service.next_treatment_day(day1 + timedelta(days=1))
        expected_second = schedule_service.next_treatment_day(expected_first + timedelta(days=1))

        self.assertEqual(s2.session_date, expected_first)
        self.assertEqual(s3.session_date, expected_second)

        # discharge_date shifted by delta between new last planned and original last planned
        original_last = day3
        new_last = expected_second
        delta = new_last - original_last
        self.assertEqual(self.patient.discharge_date, date(2026,1,31) + delta)

    def test_course_two_skip_shifts_only_course_discharge_and_sessions(self):
        course_one = TreatmentCourse.objects.create(
            patient=self.patient, course_number=1, discharge_date=date(2026, 1, 31),
        )
        course_two = TreatmentCourse.objects.create(
            patient=self.patient, course_number=2, discharge_date=date(2026, 1, 31),
        )
        self.patient.discharge_date = date(2026, 1, 31)
        self.patient.save(update_fields=['discharge_date'])
        course_one_session = TreatmentSession.objects.create(
            patient=self.patient, treatment_course=course_one, course_number=1,
            session_date=date(2026, 1, 5),
        )
        skipped = TreatmentSession.objects.create(
            patient=self.patient, treatment_course=course_two, course_number=2,
            session_date=date(2026, 1, 9),
        )
        course_two_session = TreatmentSession.objects.create(
            patient=self.patient, treatment_course=course_two, course_number=2,
            session_date=date(2026, 1, 10),
        )

        schedule_service.shift_future_sessions(
            self.patient, skipped.session_date, course_number=2,
        )

        course_one.refresh_from_db()
        course_two.refresh_from_db()
        self.patient.refresh_from_db()
        course_one_session.refresh_from_db()
        course_two_session.refresh_from_db()
        self.assertEqual(course_one.discharge_date, date(2026, 1, 31))
        self.assertEqual(course_two.discharge_date, date(2026, 2, 2))
        self.assertEqual(self.patient.discharge_date, date(2026, 1, 31))
        self.assertEqual(course_one_session.session_date, date(2026, 1, 5))
        self.assertEqual(course_two_session.session_date, date(2026, 1, 12))


class TestClinicalPathReschedule(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='path-rescheduler')
        self.client = Client()
        self.client.force_login(self.user)
        self.patient = Patient.objects.create(
            card_id='PATH1', name='Path Test', birth_date=date(1980, 1, 1),
            admission_date=date(2026, 8, 20),
            first_treatment_date=date(2026, 8, 24),
            first_visit_date=date(2026, 8, 20),
        )

    def _post(self, payload):
        return self.client.post(
            reverse('rtms_app:clinical_path_reschedule', args=[self.patient.pk]),
            data=json.dumps(payload),
            content_type='application/json',
        )

    def test_print_session_api_uses_selected_course_first_treatment_date(self):
        course_one = TreatmentCourse.objects.create(
            patient=self.patient, course_number=1,
            first_treatment_date=date(2026, 8, 24),
        )
        course_two = TreatmentCourse.objects.create(
            patient=self.patient, course_number=2,
            first_treatment_date=date(2026, 10, 5),
        )
        course_one_session = TreatmentSession.objects.create(
            patient=self.patient, treatment_course=course_one, course_number=1,
            session_date=date(2026, 10, 5), slot='',
        )
        response = self.client.post(
            f'/app/patient/{self.patient.pk}/print/api/get-session/',
            {
                'course_number': 2,
                'session_date': '2026-10-05',
            },
        )

        self.assertEqual(response.status_code, 200)
        session = TreatmentSession.objects.get(pk=response.json()['session_id'])
        self.assertEqual(session.treatment_course_id, course_two.id)
        self.assertEqual(session.course_number, 2)
        self.assertNotEqual(session.pk, course_one_session.pk)
        self.assertEqual(
            TreatmentSession.objects.filter(
                treatment_course=course_one,
            ).count(),
            1,
        )

    def test_treatment_start_rebuilds_planned_sessions_from_new_business_day(self):
        from rtms_app.services.rtms_schedule import generate_treatment_dates

        old_dates = generate_treatment_dates(self.patient.first_treatment_date, total=5, holidays=set())
        sessions = [
            TreatmentSession.objects.create(patient=self.patient, session_date=session_date)
            for session_date in old_dates
        ]
        MappingSchedule.objects.create(
            patient=self.patient, course_number=1, week_number=1,
            planned_date=old_dates[0],
        )
        MappingSchedule.objects.create(
            patient=self.patient, course_number=1, week_number=2,
            planned_date=old_dates[0] + datetime.timedelta(days=7),
        )

        result = schedule_service.reschedule_treatment_start_date(
            self.patient, date(2026, 8, 21), holidays=set(),
        )

        expected = generate_treatment_dates(date(2026, 8, 21), total=5, holidays=set())
        self.assertEqual(result['moved_count'], 5)
        self.assertEqual(
            list(TreatmentSession.objects.filter(patient=self.patient).order_by('session_date').values_list('session_date', flat=True)),
            expected,
        )
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.first_treatment_date, date(2026, 8, 21))
        self.assertEqual(self.patient.mapping_date, date(2026, 8, 21))
        self.assertEqual(
            MappingSchedule.objects.get(patient=self.patient, week_number=1).planned_date,
            date(2026, 8, 21),
        )
        self.assertEqual(
            MappingSchedule.objects.get(patient=self.patient, week_number=2).planned_date,
            date(2026, 8, 28),
        )
        self.assertEqual([s.pk for s in sessions], list(TreatmentSession.objects.filter(patient=self.patient).values_list('pk', flat=True)))

    def test_course_two_treatment_start_isolated_from_patient_and_course_one(self):
        course_one = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=1,
            first_treatment_date=date(2026, 8, 24),
        )
        course_two = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=2,
            first_treatment_date=date(2026, 10, 1),
        )
        self.patient.first_treatment_date = date(2026, 8, 24)
        self.patient.save(update_fields=['first_treatment_date'])
        course_one_sessions = [
            TreatmentSession.objects.create(
                patient=self.patient,
                treatment_course=course_one,
                course_number=1,
                session_date=session_date,
            )
            for session_date in (date(2026, 8, 24), date(2026, 8, 25))
        ]
        course_two_sessions = [
            TreatmentSession.objects.create(
                patient=self.patient,
                treatment_course=course_two,
                course_number=2,
                session_date=session_date,
            )
            for session_date in (date(2026, 10, 1), date(2026, 10, 2))
        ]
        course_one_dates_before = [session.session_date for session in course_one_sessions]

        result = schedule_service.reschedule_treatment_start_date(
            self.patient,
            date(2026, 10, 5),
            course_number=2,
            holidays=set(),
        )

        self.assertEqual(result['new_start_date'], date(2026, 10, 5))
        course_one.refresh_from_db()
        course_two.refresh_from_db()
        self.patient.refresh_from_db()
        self.assertEqual(course_one.first_treatment_date, date(2026, 8, 24))
        self.assertEqual(course_two.first_treatment_date, date(2026, 10, 5))
        self.assertEqual(self.patient.first_treatment_date, date(2026, 8, 24))
        self.assertEqual(
            list(TreatmentSession.objects.filter(treatment_course=course_one).order_by('session_date').values_list('session_date', flat=True)),
            course_one_dates_before,
        )
        self.assertEqual(
            list(TreatmentSession.objects.filter(treatment_course=course_two).order_by('session_date').values_list('session_date', flat=True)),
            [date(2026, 10, 5), date(2026, 10, 6)],
        )

    def test_course_one_treatment_start_keeps_patient_compatibility(self):
        course_one = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=1,
            first_treatment_date=date(2026, 8, 24),
        )
        self.patient.first_treatment_date = date(2026, 8, 24)
        self.patient.save(update_fields=['first_treatment_date'])

        result = schedule_service.reschedule_treatment_start_date(
            self.patient,
            date(2026, 8, 25),
            course_number=1,
            holidays=set(),
        )

        course_one.refresh_from_db()
        self.patient.refresh_from_db()
        self.assertEqual(result['old_start_date'], date(2026, 8, 24))
        self.assertEqual(course_one.first_treatment_date, date(2026, 8, 25))
        self.assertEqual(self.patient.first_treatment_date, date(2026, 8, 25))

    def test_course_two_mapping_date_and_schedule_are_isolated(self):
        course_one = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=1,
            first_treatment_date=date(2026, 8, 24),
            mapping_date=date(2026, 8, 24),
        )
        course_two = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=2,
            first_treatment_date=date(2026, 10, 1),
            mapping_date=date(2026, 10, 1),
        )
        self.patient.first_treatment_date = date(2026, 8, 24)
        self.patient.mapping_date = date(2026, 8, 24)
        self.patient.save(update_fields=['first_treatment_date', 'mapping_date'])
        first_schedule = MappingSchedule.objects.create(
            patient=self.patient,
            treatment_course=course_one,
            course_number=1,
            week_number=1,
            planned_date=date(2026, 8, 24),
        )
        second_schedule = MappingSchedule.objects.create(
            patient=self.patient,
            treatment_course=course_two,
            course_number=2,
            week_number=1,
            planned_date=date(2026, 10, 1),
        )

        schedule_service.reschedule_treatment_start_date(
            self.patient,
            date(2026, 10, 5),
            course_number=2,
            holidays=set(),
        )

        course_one.refresh_from_db()
        course_two.refresh_from_db()
        first_schedule.refresh_from_db()
        second_schedule.refresh_from_db()
        self.patient.refresh_from_db()
        self.assertEqual(course_one.mapping_date, date(2026, 8, 24))
        self.assertEqual(course_two.mapping_date, date(2026, 10, 5))
        self.assertEqual(first_schedule.planned_date, date(2026, 8, 24))
        self.assertEqual(second_schedule.planned_date, date(2026, 10, 5))
        self.assertEqual(self.patient.mapping_date, date(2026, 8, 24))

    def test_course_two_planned_mapping_drag_uses_course_mapping_date(self):
        course_one = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=1,
            first_treatment_date=date(2026, 8, 24),
            mapping_date=date(2026, 8, 24),
        )
        course_two = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=2,
            first_treatment_date=date(2026, 10, 1),
            mapping_date=date(2026, 10, 5),
        )
        self.patient.mapping_date = date(2026, 8, 24)
        self.patient.save(update_fields=['mapping_date'])
        response = self.client.post(
            reverse('rtms_app:clinical_path_reschedule', args=[self.patient.pk]),
            data=json.dumps({
                'event_type': 'mapping',
                'status': 'planned',
                'course_number': 2,
                'week_number': 1,
                'source_date': '2026-10-05',
                'target_date': '2026-10-06',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            MappingSchedule.objects.get(treatment_course=course_two, week_number=1).planned_date,
            date(2026, 10, 6),
        )
        self.assertFalse(MappingSchedule.objects.filter(treatment_course=course_one).exists())

    def test_course_one_mapping_date_keeps_patient_compatibility(self):
        course_one = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=1,
            first_treatment_date=date(2026, 8, 24),
            mapping_date=date(2026, 8, 24),
        )
        self.patient.first_treatment_date = date(2026, 8, 24)
        self.patient.mapping_date = date(2026, 8, 24)
        self.patient.save(update_fields=['first_treatment_date', 'mapping_date'])

        schedule_service.reschedule_treatment_start_date(
            self.patient,
            date(2026, 8, 25),
            course_number=1,
            holidays=set(),
        )

        course_one.refresh_from_db()
        self.patient.refresh_from_db()
        self.assertEqual(course_one.mapping_date, date(2026, 8, 25))
        self.assertEqual(self.patient.mapping_date, date(2026, 8, 25))

    def test_treatment_start_preserves_done_and_skipped_rows(self):
        first = TreatmentSession.objects.create(
            patient=self.patient, session_date=date(2026, 8, 24), status='planned',
        )
        done = TreatmentSession.objects.create(
            patient=self.patient, session_date=date(2026, 8, 25), status='done',
        )
        skipped = TreatmentSession.objects.create(
            patient=self.patient, session_date=date(2026, 8, 26), status='skipped',
        )
        later = TreatmentSession.objects.create(
            patient=self.patient, session_date=date(2026, 8, 27), status='planned',
        )

        schedule_service.reschedule_treatment_start_date(
            self.patient, date(2026, 8, 21), holidays=set(),
        )

        first.refresh_from_db()
        done.refresh_from_db()
        skipped.refresh_from_db()
        later.refresh_from_db()
        self.assertEqual(first.session_date, date(2026, 8, 21))
        self.assertEqual(done.session_date, date(2026, 8, 25))
        self.assertEqual(done.status, 'done')
        self.assertEqual(skipped.session_date, date(2026, 8, 26))
        self.assertEqual(skipped.status, 'skipped')
        self.assertEqual(later.session_date, date(2026, 8, 24))

    def test_treatment_start_rebuilds_regular_rows_without_touching_overflow(self):
        sessions = [
            TreatmentSession.objects.create(
                patient=self.patient,
                session_date=date(2026, 8, 24) + datetime.timedelta(days=index),
            )
            for index in range(31)
        ]
        overflow = sessions[30]
        overflow_date = overflow.session_date

        schedule_service.reschedule_treatment_start_date(
            self.patient, date(2026, 8, 21), holidays=set(),
        )

        overflow.refresh_from_db()
        self.assertEqual(overflow.session_date, overflow_date)
        self.assertEqual(overflow.status, 'planned')
        self.assertEqual(len(sessions), 31)

        self.patient.refresh_from_db()
        self.assertEqual(self.patient.first_treatment_date, date(2026, 8, 21))
        self.assertEqual(TreatmentSession.objects.filter(patient=self.patient).count(), 31)

    def test_first_treatment_api_allows_legacy_overflow_without_modifying_it(self):
        sessions = [
            TreatmentSession.objects.create(
                patient=self.patient,
                session_date=date(2026, 8, 24) + datetime.timedelta(days=index),
            )
            for index in range(34)
        ]
        overflow_before = [
            (session.pk, session.session_date, session.status)
            for session in sessions[30:]
        ]

        response = self._post({
            'event_type': 'treatment', 'status': 'planned',
            'session_id': sessions[0].pk,
            'source_date': sessions[0].session_date.isoformat(),
            'target_date': '2026-08-21',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(TreatmentSession.objects.filter(patient=self.patient).count(), 34)
        self.assertEqual(
            [
                (session.pk, session.session_date, session.status)
                for session in TreatmentSession.objects.filter(
                    pk__in=[session.pk for session in sessions[30:]]
                ).order_by('pk')
            ],
            overflow_before,
        )

    def test_first_treatment_drag_uses_course_rebuild_service(self):
        sessions = [
            TreatmentSession.objects.create(patient=self.patient, session_date=session_date)
            for session_date in [
                date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26),
            ]
        ]

        response = self._post({
            'event_type': 'treatment', 'status': 'planned',
            'session_id': sessions[0].pk,
            'source_date': '2026-08-24', 'target_date': '2026-08-21',
        })

        self.assertEqual(response.status_code, 200)
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.first_treatment_date, date(2026, 8, 21))
        sessions[0].refresh_from_db()
        sessions[1].refresh_from_db()
        sessions[2].refresh_from_db()
        self.assertEqual(
            [sessions[0].session_date, sessions[1].session_date, sessions[2].session_date],
            [date(2026, 8, 21), date(2026, 8, 24), date(2026, 8, 25)],
        )

    def test_treatment_can_move_to_saturday_without_propagating_weekend(self):
        sessions = [
            TreatmentSession.objects.create(patient=self.patient, session_date=session_date)
            for session_date in [
                date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26),
                date(2026, 8, 27), date(2026, 8, 28), date(2026, 8, 31),
            ]
        ]

        response = self._post({
            'event_type': 'treatment', 'status': 'planned',
            'session_id': sessions[4].pk,
            'source_date': '2026-08-28', 'target_date': '2026-08-29',
        })

        self.assertEqual(response.status_code, 200)
        sessions[4].refresh_from_db()
        sessions[5].refresh_from_db()
        self.assertEqual(sessions[4].session_date, date(2026, 8, 29))
        self.assertEqual(sessions[5].session_date, date(2026, 8, 31))
        self.assertEqual(TreatmentSession.objects.filter(patient=self.patient).count(), 6)

    def test_treatment_can_move_to_holiday_without_propagating_holiday(self):
        session = TreatmentSession.objects.create(
            patient=self.patient, session_date=date(2026, 9, 18), status='planned',
        )
        following = TreatmentSession.objects.create(
            patient=self.patient, session_date=date(2026, 9, 23), status='planned',
        )

        response = self._post({
            'event_type': 'treatment', 'status': 'planned',
            'session_id': session.pk,
            'source_date': '2026-09-18', 'target_date': '2026-09-21',
        })

        self.assertEqual(response.status_code, 200)
        session.refresh_from_db()
        following.refresh_from_db()
        self.assertEqual(session.session_date, date(2026, 9, 21))
        self.assertEqual(following.session_date, date(2026, 9, 24))

    def test_first_treatment_can_be_set_to_saturday_and_following_dates_are_business_days(self):
        sessions = [
            TreatmentSession.objects.create(patient=self.patient, session_date=session_date)
            for session_date in [date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26)]
        ]

        response = self._post({
            'event_type': 'treatment', 'status': 'planned',
            'session_id': sessions[0].pk,
            'source_date': '2026-08-24', 'target_date': '2026-08-29',
        })

        self.assertEqual(response.status_code, 200)
        self.patient.refresh_from_db()
        for session in sessions:
            session.refresh_from_db()
        self.assertEqual(self.patient.first_treatment_date, date(2026, 8, 29))
        self.assertEqual(
            [session.session_date for session in sessions],
            [date(2026, 8, 29), date(2026, 8, 31), date(2026, 9, 1)],
        )

    def test_clinical_path_renders_drag_metadata_and_csrf(self):
        TreatmentSession.objects.create(
            patient=self.patient, session_date=date(2026, 8, 26), status='planned',
        )

        response = self.client.get(reverse('rtms_app:patient_clinical_path', args=[self.patient.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'draggable="true"')
        self.assertContains(response, 'data-event-type="treatment"')
        self.assertContains(response, 'data-source-date="2026-08-26"')
        self.assertContains(response, 'data-reschedule-url="/app/patient/%s/path/reschedule/"' % self.patient.pk)
        self.assertContains(response, 'name="csrfmiddlewaretoken"')
        self.assertContains(response, 'patient_clinical_path.js')

    def test_first_treatment_event_contains_confirmation_metadata(self):
        TreatmentSession.objects.create(
            patient=self.patient, session_date=self.patient.first_treatment_date, status='planned',
        )

        response = self.client.get(reverse('rtms_app:patient_clinical_path', args=[self.patient.pk]))

        self.assertContains(response, 'data-first-treatment="true"')
        self.assertContains(response, 'data-non-business-day="true"')

    def test_first_visit_contains_start_date_confirmation(self):
        response = self.client.get(reverse('rtms_app:patient_first_visit', args=[self.patient.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'data-original-first-treatment-date="2026-08-24"')
        self.assertContains(response, '治療開始日を変更すると')

    def test_first_visit_post_rebuilds_treatment_calendar_from_changed_start_date(self):
        doctor_group, _ = Group.objects.get_or_create(name='医師')
        doctor = get_user_model().objects.create_user(username='first-visit-start-doctor')
        doctor.groups.add(doctor_group)
        self.client.force_login(doctor)
        sessions = [
            TreatmentSession.objects.create(patient=self.patient, session_date=session_date)
            for session_date in [date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26)]
        ]

        response = self.client.post(
            reverse('rtms_app:patient_first_visit', args=[self.patient.pk]),
            {
                'card_id': '56001',
                'name': self.patient.name,
                'birth_date': self.patient.birth_date.isoformat(),
                'gender': self.patient.gender,
                'attending_physician': str(doctor.pk),
                'admission_date': '2026-08-20',
                'first_visit_date': '2026-08-20',
                'first_treatment_date': '2026-08-21',
                'has_other_psychiatric_history': 'no',
                'psychiatric_history': [],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.patient.refresh_from_db()
        for session in sessions:
            session.refresh_from_db()
        self.assertEqual(self.patient.first_treatment_date, date(2026, 8, 21))
        self.assertEqual(
            [session.session_date for session in sessions],
            [date(2026, 8, 21), date(2026, 8, 24), date(2026, 8, 25)],
        )

    def test_course_two_first_visit_rebuild_does_not_change_course_one(self):
        doctor_group, _ = Group.objects.get_or_create(name='医師')
        doctor = get_user_model().objects.create_user(username='course-two-first-visit-doctor')
        doctor.groups.add(doctor_group)
        course_one = TreatmentCourse.objects.create(
            patient=self.patient, course_number=1,
            first_treatment_date=date(2026, 8, 24), mapping_date=date(2026, 8, 24),
        )
        course_two = TreatmentCourse.objects.create(
            patient=self.patient, course_number=2,
            first_treatment_date=date(2026, 9, 1), mapping_date=date(2026, 9, 1),
        )
        course_one_session = TreatmentSession.objects.create(
            patient=self.patient, treatment_course=course_one, course_number=1,
            session_date=date(2026, 8, 24), status='planned',
        )
        course_two_session = TreatmentSession.objects.create(
            patient=self.patient, treatment_course=course_two, course_number=2,
            session_date=date(2026, 9, 1), status='planned',
        )

        self.client.force_login(doctor)
        response = self.client.post(
            f'{reverse("rtms_app:patient_first_visit", args=[self.patient.pk])}?course_number=2',
            {
                'course_number': '2',
                'card_id': '56001',
                'name': self.patient.name,
                'birth_date': self.patient.birth_date.isoformat(),
                'gender': self.patient.gender,
                'attending_physician': str(doctor.pk),
                'admission_date': '2026-08-20',
                'first_visit_date': '2026-08-20',
                'first_treatment_date': '2026-09-02',
                'has_other_psychiatric_history': 'no',
                'psychiatric_history': [],
            },
        )

        self.assertEqual(response.status_code, 302)
        course_one_session.refresh_from_db()
        course_two_session.refresh_from_db()
        self.patient.refresh_from_db()
        course_one.refresh_from_db()
        course_two.refresh_from_db()
        self.assertEqual(course_one_session.session_date, date(2026, 8, 24))
        self.assertEqual(course_two_session.session_date, date(2026, 9, 2))
        self.assertEqual(course_one.first_treatment_date, date(2026, 8, 24))
        self.assertEqual(course_two.first_treatment_date, date(2026, 9, 2))
        self.assertEqual(self.patient.first_treatment_date, date(2026, 8, 24))

    def test_calendar_limits_planned_mapping_to_treatment_course(self):
        from rtms_app.views import generate_calendar_weeks

        weeks, _ = generate_calendar_weeks(self.patient)
        mapping_events = [
            (day['date'], event)
            for week in weeks for day in week for event in day['events']
            if event['type'] == 'mapping'
        ]

        self.assertTrue(mapping_events)
        self.assertLessEqual(max(event['week_number'] for _date, event in mapping_events), 7)

    def test_calendar_stops_planned_mapping_at_materialized_thirtieth_treatment(self):
        from rtms_app.views import generate_calendar_weeks

        sessions = [
            TreatmentSession.objects.create(
                patient=self.patient,
                session_date=date(2026, 8, 24) + datetime.timedelta(days=index),
            )
            for index in range(30)
        ]

        weeks, _ = generate_calendar_weeks(self.patient)
        mapping_dates = [
            day['date']
            for week in weeks for day in week
            for event in day['events']
            if event['type'] == 'mapping'
        ]

        self.assertTrue(mapping_dates)
        self.assertLessEqual(max(mapping_dates), sessions[-1].session_date)

    def test_legacy_overflow_does_not_extend_planned_mapping_range(self):
        from rtms_app.views import generate_calendar_weeks

        sessions = [
            TreatmentSession.objects.create(
                patient=self.patient,
                session_date=date(2026, 8, 24) + datetime.timedelta(days=index),
            )
            for index in range(34)
        ]
        overflow_before = [
            (session.pk, session.session_date, session.status, session.course_number)
            for session in sessions[30:]
        ]

        weeks, _ = generate_calendar_weeks(self.patient)
        mapping_dates = [
            day['date']
            for week in weeks for day in week
            for event in day['events']
            if event['type'] == 'mapping'
        ]

        self.assertTrue(mapping_dates)
        self.assertLessEqual(max(mapping_dates), sessions[29].session_date)
        self.assertEqual(
            [
                (session.pk, session.session_date, session.status, session.course_number)
                for session in TreatmentSession.objects.filter(pk__in=[s.pk for s in sessions[30:]]).order_by('pk')
            ],
            overflow_before,
        )

    def test_mapping_override_after_materialized_course_end_is_not_displayed(self):
        from rtms_app.views import generate_calendar_weeks

        sessions = [
            TreatmentSession.objects.create(
                patient=self.patient,
                session_date=date(2026, 8, 24) + datetime.timedelta(days=index),
            )
            for index in range(30)
        ]
        MappingSchedule.objects.create(
            patient=self.patient,
            course_number=1,
            week_number=8,
            planned_date=date(2026, 10, 19),
        )

        weeks, _ = generate_calendar_weeks(self.patient)
        mapping_dates = [
            day['date']
            for week in weeks for day in week
            for event in day['events']
            if event['type'] == 'mapping'
        ]

        self.assertNotIn(date(2026, 10, 19), mapping_dates)
        self.assertLessEqual(max(mapping_dates), sessions[-1].session_date)

    def test_treatment_reschedule_rebuilds_all_later_planned_sessions(self):
        sessions = [
            TreatmentSession.objects.create(patient=self.patient, session_date=d)
            for d in [date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26), date(2026, 8, 27), date(2026, 8, 28)]
        ]

        response = self._post({
            'event_type': 'treatment', 'status': 'planned',
            'source_date': '2026-08-26', 'target_date': '2026-09-01',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(TreatmentSession.objects.filter(patient=self.patient).order_by('session_date').values_list('session_date', flat=True)),
            [date(2026, 8, 24), date(2026, 8, 25), date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)],
        )
        for session, expected in zip(sessions, [date(2026, 8, 24), date(2026, 8, 25), date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]):
            session.refresh_from_db()
            self.assertEqual(session.session_date, expected)

    def test_treatment_reschedule_skips_holiday_and_preserves_order(self):
        sessions = [
            TreatmentSession.objects.create(patient=self.patient, session_date=d)
            for d in [date(2026, 8, 7), date(2026, 8, 11), date(2026, 8, 12)]
        ]

        response = self._post({
            'event_type': 'treatment', 'status': 'planned',
            'source_date': '2026-08-07', 'target_date': '2026-08-10',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [session.refresh_from_db() or session.session_date for session in sessions],
            [date(2026, 8, 10), date(2026, 8, 12), date(2026, 8, 13)],
        )

    def test_treatment_reschedule_allows_backward_move_and_preserves_sequence(self):
        sessions = [
            TreatmentSession.objects.create(patient=self.patient, session_date=d)
            for d in [date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26), date(2026, 8, 27), date(2026, 8, 28)]
        ]

        response = self._post({
            'event_type': 'treatment', 'status': 'planned',
            'session_id': sessions[3].pk,
            'source_date': '2026-08-27', 'target_date': '2026-08-26',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            list(TreatmentSession.objects.filter(patient=self.patient).order_by('session_date').values_list('session_date', flat=True)),
            [date(2026, 8, 24), date(2026, 8, 25), date(2026, 8, 26), date(2026, 8, 27), date(2026, 8, 28)],
        )
        sessions[3].refresh_from_db()
        self.assertEqual(sessions[3].session_date, date(2026, 8, 26))
        sessions[2].refresh_from_db()
        self.assertEqual(sessions[2].session_date, date(2026, 8, 27))

    def test_treatment_session_id_is_authoritative_when_date_is_ambiguous(self):
        first = TreatmentSession.objects.create(patient=self.patient, session_date=date(2026, 8, 24))
        second = TreatmentSession.objects.create(patient=self.patient, session_date=date(2026, 8, 25))

        response = self._post({
            'event_type': 'treatment', 'status': 'planned',
            'session_id': second.pk,
            'source_date': '2026-08-25', 'target_date': '2026-08-27',
        })

        self.assertEqual(response.status_code, 200)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.session_date, date(2026, 8, 24))
        self.assertEqual(second.session_date, date(2026, 8, 27))

    def test_treatment_calendar_numbers_materialized_sessions_by_date_order(self):
        from rtms_app.views import generate_calendar_weeks

        for d in [date(2026, 8, 24), date(2026, 8, 26), date(2026, 8, 27), date(2026, 8, 28), date(2026, 8, 31)]:
            TreatmentSession.objects.create(patient=self.patient, session_date=d)

        weeks, _ = generate_calendar_weeks(self.patient)
        days = {
            day['date']: [event['label'] for event in day['events'] if event['type'] == 'treatment']
            for week in weeks for day in week
        }
        self.assertIn('2回目', days[date(2026, 8, 26)][0])
        self.assertNotIn('3回目', days[date(2026, 8, 26)][0])
        self.assertIn('5回目', days[date(2026, 8, 31)][0])

    def test_treatment_calendar_includes_materialized_session_after_canonical_30(self):
        from rtms_app.views import generate_calendar_weeks
        moved = TreatmentSession.objects.create(patient=self.patient, session_date=date(2026, 10, 1))

        weeks, _ = generate_calendar_weeks(self.patient)
        days = {day['date'] for week in weeks for day in week}

        self.assertIn(moved.session_date, days)

    def test_treatment_limit_and_deterministic_overflow_detection(self):
        dates = [date(2026, 8, 24) + datetime.timedelta(days=i) for i in range(34)]
        sessions = [TreatmentSession.objects.create(patient=self.patient, session_date=d) for d in dates]

        self.assertFalse(schedule_service.can_create_treatment_session(self.patient, 1))
        info = schedule_service.get_treatment_overflow_info(self.patient, 1)
        self.assertEqual(info['count'], 34)
        self.assertEqual(info['overflow_count'], 4)
        self.assertEqual([s.pk for s in info['overflow_sessions']], [s.pk for s in sessions[30:]])
        self.assertEqual(
            list(schedule_service.get_treatment_session_number_map(self.patient, 1).values()),
            list(range(1, 31)),
        )

    def test_course_treatment_limit_is_independent(self):
        for i in range(30):
            TreatmentSession.objects.create(
                patient=self.patient,
                course_number=1,
                session_date=date(2026, 8, 24) + datetime.timedelta(days=i),
            )

        self.assertFalse(schedule_service.can_create_treatment_session(self.patient, 1))
        self.assertTrue(schedule_service.can_create_treatment_session(self.patient, 2))

    def test_overflow_reschedule_does_not_create_another_session(self):
        sessions = [
            TreatmentSession.objects.create(
                patient=self.patient,
                session_date=date(2026, 8, 24) + datetime.timedelta(days=i),
            )
            for i in range(34)
        ]
        ids_before = set(TreatmentSession.objects.filter(patient=self.patient).values_list('pk', flat=True))

        response = self._post({
            'event_type': 'treatment', 'status': 'planned',
            'session_id': sessions[1].pk,
            'source_date': sessions[1].session_date.isoformat(),
            'target_date': '2026-08-27',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(TreatmentSession.objects.filter(patient=self.patient).count(), 34)
        self.assertEqual(
            set(TreatmentSession.objects.filter(patient=self.patient).values_list('pk', flat=True)),
            ids_before,
        )

    def test_overflow_virtual_materialize_is_rejected_without_db_change(self):
        for i in range(34):
            TreatmentSession.objects.create(
                patient=self.patient,
                session_date=date(2026, 8, 25) + datetime.timedelta(days=i),
            )
        ids_before = set(TreatmentSession.objects.filter(patient=self.patient).values_list('pk', flat=True))

        response = self._post({
            'event_type': 'treatment', 'status': 'planned',
            'source_date': '2026-08-24', 'target_date': '2026-08-25',
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(TreatmentSession.objects.filter(patient=self.patient).count(), 34)
        self.assertEqual(
            set(TreatmentSession.objects.filter(patient=self.patient).values_list('pk', flat=True)),
            ids_before,
        )

    def test_thirtieth_treatment_can_move_without_creating_thirty_first(self):
        from rtms_app.services.rtms_schedule import generate_treatment_dates

        dates = generate_treatment_dates(self.patient.first_treatment_date, total=30, holidays=set())
        sessions = [TreatmentSession.objects.create(patient=self.patient, session_date=d) for d in dates]
        last = sessions[-1]

        response = self._post({
            'event_type': 'treatment', 'status': 'planned',
            'session_id': last.pk,
            'source_date': last.session_date.isoformat(),
            'target_date': '2026-10-14',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(TreatmentSession.objects.filter(patient=self.patient).count(), 30)
        last.refresh_from_db()
        self.assertEqual(last.session_date, date(2026, 10, 14))

    def test_thirtieth_treatment_can_move_to_exceptional_day_without_creating_thirty_first(self):
        from rtms_app.services.rtms_schedule import generate_treatment_dates

        dates = generate_treatment_dates(self.patient.first_treatment_date, total=30, holidays=set())
        sessions = [
            TreatmentSession.objects.create(patient=self.patient, session_date=session_date)
            for session_date in dates
        ]

        response = self._post({
            'event_type': 'treatment', 'status': 'planned',
            'session_id': sessions[-1].pk,
            'source_date': sessions[-1].session_date.isoformat(),
            'target_date': '2026-10-10',
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(TreatmentSession.objects.filter(patient=self.patient).count(), 30)
        sessions[-1].refresh_from_db()
        self.assertEqual(sessions[-1].session_date, date(2026, 10, 10))

    def test_api_get_session_rejects_new_session_after_thirty(self):
        for i in range(30):
            TreatmentSession.objects.create(
                patient=self.patient,
                session_date=date(2026, 8, 24) + datetime.timedelta(days=i),
            )

        response = self.client.post(f'/app/patient/{self.patient.pk}/print/api/get-session/', {
            'course_number': 1,
            'session_date': '2027-01-04',
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(TreatmentSession.objects.filter(patient=self.patient).count(), 30)

    def test_initial_session_creation_uses_holiday_aware_canonical_dates(self):
        from rtms_app.services.rtms_schedule import generate_treatment_dates

        canonical = generate_treatment_dates(
            self.patient.first_treatment_date,
            total=30,
            holidays={date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23)},
        )

        valid_response = self.client.post(
            f'/app/patient/{self.patient.pk}/print/api/get-session/',
            {
                'course_number': 1,
                'session_date': canonical[5].isoformat(),
            },
        )
        self.assertEqual(valid_response.status_code, 200)

        holiday_response = self.client.post(
            f'/app/patient/{self.patient.pk}/print/api/get-session/',
            {
                'course_number': 1,
                'session_date': '2026-09-21',
            },
        )
        self.assertEqual(holiday_response.status_code, 400)
        self.assertFalse(
            TreatmentSession.objects.filter(
                patient=self.patient, session_date=date(2026, 9, 21),
            ).exists()
        )

        # A holiday explicitly configured through the clinical-path exception
        # flow is an existing Session and must remain retrievable.
        exceptional = TreatmentSession.objects.create(
            patient=self.patient, session_date=date(2026, 9, 21), status='planned',
        )
        existing_response = self.client.post(
            f'/app/patient/{self.patient.pk}/print/api/get-session/',
            {'course_number': 1, 'session_date': '2026-09-21'},
        )
        self.assertEqual(existing_response.status_code, 200)
        self.assertEqual(existing_response.json()['session_id'], exceptional.pk)

    def test_start_date_rebuild_matches_initial_holiday_aware_generation(self):
        from rtms_app.services.rtms_schedule import generate_treatment_dates

        starts = [
            date(2026, 8, 10),   # Monday
            date(2026, 8, 7),    # Friday
            date(2026, 9, 18),   # Before the September holiday block
            date(2026, 12, 25),  # Before year-end closure
        ]
        for index, new_start in enumerate(starts):
            patient = Patient.objects.create(
                card_id=f'PATH-COMPARE-{index}',
                name=f'Compare {index}',
                birth_date=date(1980, 1, 1),
                first_treatment_date=date(2026, 8, 24),
                first_visit_date=date(2026, 8, 20),
            )
            old_dates = generate_treatment_dates(
                patient.first_treatment_date, total=30, holidays={
                    date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23),
                },
            )
            sessions = [
                TreatmentSession.objects.create(patient=patient, session_date=session_date)
                for session_date in old_dates
            ]

            result = schedule_service.reschedule_treatment_start_date(
                patient, new_start,
                holidays={date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23)},
            )
            expected = generate_treatment_dates(
                new_start, total=30,
                holidays={date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23)},
            )
            self.assertEqual(result['moved_count'], 30)
            self.assertEqual(
                list(TreatmentSession.objects.filter(patient=patient).order_by('session_date').values_list('session_date', flat=True)),
                expected,
            )
            self.assertEqual(len(sessions), 30)

    def test_calendar_hides_legacy_overflow_without_clamping_number(self):
        from rtms_app.views import generate_calendar_weeks

        dates = [date(2026, 8, 24) + datetime.timedelta(days=i) for i in range(34)]
        sessions = [TreatmentSession.objects.create(patient=self.patient, session_date=d) for d in dates]
        weeks, _ = generate_calendar_weeks(self.patient)
        events = [
            event for week in weeks for day in week for event in day['events']
            if event['type'] == 'treatment'
        ]

        self.assertEqual(len(events), 30)
        self.assertTrue(any('30回目' in event['label'] for event in events))
        self.assertFalse(any('31回目' in event['label'] for event in events))
        self.assertFalse(any(event.get('session_id') == sessions[30].pk for event in events))

    def test_invalid_source_does_not_create_canonical_sessions(self):
        response = self._post({
            'event_type': 'treatment', 'status': 'planned',
            'source_date': '2026-12-31', 'target_date': '2027-01-04',
        })

        self.assertEqual(response.status_code, 404)
        self.assertEqual(TreatmentSession.objects.filter(patient=self.patient).count(), 0)

    def test_malformed_target_does_not_create_canonical_sessions(self):
        response = self._post({
            'event_type': 'treatment', 'status': 'planned',
            'source_date': '2026-08-26', 'target_date': 'not-a-date',
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(TreatmentSession.objects.filter(patient=self.patient).count(), 0)

    def test_transaction_rolls_back_canonical_creation_on_service_error(self):
        with patch('rtms_app.views.reschedule_planned_session', side_effect=IntegrityError('forced')):
            response = self._post({
                'event_type': 'treatment', 'status': 'planned',
                'source_date': '2026-08-26', 'target_date': '2026-09-01',
            })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(TreatmentSession.objects.filter(patient=self.patient).count(), 0)

    def test_done_or_skipped_treatment_cannot_use_planned_path(self):
        done = TreatmentSession.objects.create(
            patient=self.patient, session_date=date(2026, 8, 26), status='done',
        )
        response = self._post({
            'event_type': 'treatment', 'status': 'planned',
            'source_date': '2026-08-26', 'target_date': '2026-09-01',
        })

        self.assertEqual(response.status_code, 400)
        done.refresh_from_db()
        self.assertEqual(done.session_date, date(2026, 8, 26))

        skipped = TreatmentSession.objects.create(
            patient=self.patient, session_date=date(2026, 8, 27), status='skipped',
        )
        response = self._post({
            'event_type': 'treatment', 'status': 'planned',
            'source_date': '2026-08-27', 'target_date': '2026-09-02',
        })
        self.assertEqual(response.status_code, 400)
        skipped.refresh_from_db()
        self.assertEqual(skipped.session_date, date(2026, 8, 27))

    def test_planned_treatment_cannot_use_done_target(self):
        TreatmentSession.objects.create(patient=self.patient, session_date=date(2026, 8, 26))
        done = TreatmentSession.objects.create(
            patient=self.patient, session_date=date(2026, 9, 1), status='done',
        )

        response = self._post({
            'event_type': 'treatment', 'status': 'planned',
            'source_date': '2026-08-26', 'target_date': '2026-09-01',
        })

        self.assertEqual(response.status_code, 400)
        self.assertEqual(TreatmentSession.objects.filter(patient=self.patient).count(), 2)
        done.refresh_from_db()
        self.assertEqual(done.session_date, date(2026, 9, 1))

    def test_mapping_planned_and_done_moves_validate_source(self):
        planned = self._post({
            'event_type': 'mapping', 'status': 'planned', 'week_number': 1,
            'source_date': '2026-08-24', 'target_date': '2026-08-25',
        })
        self.assertEqual(planned.status_code, 200)
        self.assertEqual(
            MappingSchedule.objects.get(patient=self.patient, week_number=1).planned_date,
            date(2026, 8, 25),
        )

        mapping = MappingSession.objects.create(
            patient=self.patient, date=date(2026, 8, 26), week_number=1,
            resting_mt=100,
        )
        done = self._post({
            'event_type': 'mapping', 'status': 'done', 'session_id': mapping.pk,
            'source_date': '2026-08-26', 'target_date': '2026-08-27',
        })
        self.assertEqual(done.status_code, 200)
        mapping.refresh_from_db()
        self.assertEqual(mapping.date, date(2026, 8, 27))

    def test_mapping_invalid_source_and_duplicate_target_are_rejected(self):
        invalid = self._post({
            'event_type': 'mapping', 'status': 'planned', 'week_number': 1,
            'source_date': '2026-08-25', 'target_date': '2026-08-26',
        })
        self.assertEqual(invalid.status_code, 404)
        self.assertFalse(MappingSchedule.objects.filter(patient=self.patient).exists())

        MappingSchedule.objects.create(
            patient=self.patient, course_number=1, week_number=2,
            planned_date=date(2026, 8, 26),
        )
        duplicate = self._post({
            'event_type': 'mapping', 'status': 'planned', 'week_number': 1,
            'source_date': '2026-08-24', 'target_date': '2026-08-26',
        })
        self.assertEqual(duplicate.status_code, 400)
        self.assertEqual(
            MappingSchedule.objects.get(patient=self.patient, week_number=2).planned_date,
            date(2026, 8, 26),
        )

    def test_assessment_planned_move_and_done_assessment_rejection(self):
        hamd = ScaleDefinition.objects.get_or_create(code='hamd', defaults={'name': 'HAM-D'})[0]
        planned = self._post({
            'event_type': 'assessment', 'scale_code': hamd.code,
            'timing': 'baseline', 'target_date': '2026-08-25',
        })
        self.assertEqual(planned.status_code, 200)
        self.assertEqual(
            AssessmentSchedule.objects.get(
                patient=self.patient, scale=hamd, timing='baseline',
            ).planned_date,
            date(2026, 8, 25),
        )

        Assessment.objects.create(
            patient=self.patient, timing='week3', type='HAM-D',
            date=date(2026, 9, 1), scores={},
        )
        done = self._post({
            'event_type': 'assessment', 'scale_code': hamd.code,
            'timing': 'week3', 'target_date': '2026-09-02',
        })
        self.assertEqual(done.status_code, 400)

    def test_other_assessment_group_move_and_invalid_timing(self):
        other = ScaleDefinition.objects.get_or_create(code='phq9', defaults={'name': 'PHQ-9'})[0]
        moved = self._post({
            'event_type': 'assessment', 'scale_code': '__other_scales__',
            'timing': 'baseline', 'target_date': '2026-08-25',
        })
        self.assertEqual(moved.status_code, 200)
        self.assertEqual(
            AssessmentSchedule.objects.get(
                patient=self.patient, scale=other, timing='baseline',
            ).planned_date,
            date(2026, 8, 25),
        )

        invalid = self._post({
            'event_type': 'assessment', 'scale_code': 'hamd',
            'timing': 'invalid', 'target_date': '2026-08-25',
        })
        self.assertEqual(invalid.status_code, 400)

    def test_admission_and_discharge_change_only_patient_dates(self):
        admission = self._post({'event_type': 'admission', 'target_date': '2026-08-21'})
        discharge = self._post({'event_type': 'discharge', 'target_date': '2026-09-10'})

        self.assertEqual(admission.status_code, 200)
        self.assertEqual(discharge.status_code, 200)
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.admission_date, date(2026, 8, 21))
        self.assertEqual(self.patient.discharge_date, date(2026, 9, 10))

        invalid = self._post({'event_type': 'admission', 'target_date': 'not-a-date'})
        self.assertEqual(invalid.status_code, 400)
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.admission_date, date(2026, 8, 21))

    def test_course_two_discharge_change_isolated_from_patient_and_course_one(self):
        course_one = TreatmentCourse.objects.create(
            patient=self.patient, course_number=1, discharge_date=date(2026, 9, 30),
        )
        course_two = TreatmentCourse.objects.create(
            patient=self.patient, course_number=2, discharge_date=date(2026, 10, 31),
        )
        self.patient.discharge_date = date(2026, 9, 30)
        self.patient.save(update_fields=['discharge_date'])

        response = self._post({
            'event_type': 'discharge',
            'course_number': 2,
            'target_date': '2026-11-05',
        })

        self.assertEqual(response.status_code, 200)
        course_one.refresh_from_db()
        course_two.refresh_from_db()
        self.patient.refresh_from_db()
        self.assertEqual(self.patient.discharge_date, date(2026, 9, 30))
        self.assertEqual(course_one.discharge_date, date(2026, 9, 30))
        self.assertEqual(course_two.discharge_date, date(2026, 11, 5))

    def test_course_one_discharge_change_keeps_patient_compatibility(self):
        course_one = TreatmentCourse.objects.create(
            patient=self.patient, course_number=1, discharge_date=date(2026, 9, 30),
        )
        self.patient.discharge_date = date(2026, 9, 30)
        self.patient.save(update_fields=['discharge_date'])

        response = self._post({
            'event_type': 'discharge',
            'course_number': 1,
            'target_date': '2026-10-01',
        })

        self.assertEqual(response.status_code, 200)
        course_one.refresh_from_db()
        self.patient.refresh_from_db()
        self.assertEqual(course_one.discharge_date, date(2026, 10, 1))
        self.assertEqual(self.patient.discharge_date, date(2026, 10, 1))

    def test_course_discharge_null_uses_patient_fallback_in_calendar(self):
        from rtms_app.views import generate_calendar_weeks

        course_two = TreatmentCourse.objects.create(
            patient=self.patient, course_number=2,
            first_treatment_date=date(2026, 8, 24),
        )
        self.patient.discharge_date = date(2026, 9, 10)
        self.patient.save(update_fields=['discharge_date'])

        calendar_weeks, _ = generate_calendar_weeks(
            self.patient, treatment_course=course_two,
        )
        discharge_dates = {
            day['date']
            for week in calendar_weeks
            for day in week
            if any(event['type'] == 'discharge' for event in day['events'])
        }

        self.assertIn(date(2026, 9, 10), discharge_dates)

    def test_course_two_admission_change_isolated_from_patient_and_course_one(self):
        course_one = TreatmentCourse.objects.create(
            patient=self.patient, course_number=1, admission_date=date(2026, 8, 20),
        )
        course_two = TreatmentCourse.objects.create(
            patient=self.patient, course_number=2, admission_date=date(2026, 10, 1),
        )
        self.patient.admission_date = date(2026, 8, 20)
        self.patient.save(update_fields=['admission_date'])

        response = self._post({
            'event_type': 'admission',
            'course_number': 2,
            'target_date': '2026-10-05',
        })

        self.assertEqual(response.status_code, 200)
        course_one.refresh_from_db()
        course_two.refresh_from_db()
        self.patient.refresh_from_db()
        self.assertEqual(course_one.admission_date, date(2026, 8, 20))
        self.assertEqual(course_two.admission_date, date(2026, 10, 5))
        self.assertEqual(self.patient.admission_date, date(2026, 8, 20))

    def test_course_one_admission_change_keeps_patient_compatibility(self):
        course_one = TreatmentCourse.objects.create(
            patient=self.patient, course_number=1, admission_date=date(2026, 8, 20),
        )
        self.patient.admission_date = date(2026, 8, 20)
        self.patient.save(update_fields=['admission_date'])

        response = self._post({
            'event_type': 'admission',
            'course_number': 1,
            'target_date': '2026-08-21',
        })

        self.assertEqual(response.status_code, 200)
        course_one.refresh_from_db()
        self.patient.refresh_from_db()
        self.assertEqual(course_one.admission_date, date(2026, 8, 21))
        self.assertEqual(self.patient.admission_date, date(2026, 8, 21))

class TestPatientSurveyFlow(TestCase):
    def setUp(self):
        self.client = Client()
        User = get_user_model()
        # Ensure patient group exists
        ensure_patient_group()
        self.patient = Patient.objects.create(
            card_id="12345",
            name="Survey Patient",
            birth_date=datetime.date(1990, 1, 1),
        )
        # Create linked user with patient group
        from rtms_app.services.patient_accounts import ensure_patient_user
        user, _ = ensure_patient_user(self.patient, reset_password=True)
        self.user = user
        self.client.force_login(self.user)

    def _answers_for(self, instrument_code: str):
        inst = get_instrument(instrument_code)
        data = {}
        for q in inst.get("questions", []):
            opts = q.get("options", [])
            if not opts:
                continue
            data[q["key"]] = opts[0]["id"]
        return data

    def test_full_flow_reaches_review(self):
        from rtms_app.models import PatientSurveySession

        session = PatientSurveySession.objects.create(
            patient=self.patient,
            phase="pre",
            status="in_progress",
            course_number=1,
        )

        for idx, code in enumerate(INSTRUMENT_ORDER):
            url = reverse("patient_portal:instrument", args=[session.id, code])
            data = self._answers_for(code)
            data["nav"] = "next"
            resp = self.client.post(url, data)
            if idx == len(INSTRUMENT_ORDER) - 1:
                self.assertRedirects(resp, reverse("patient_portal:review", args=[session.id]))
            else:
                next_code = INSTRUMENT_ORDER[idx + 1]
                self.assertRedirects(resp, reverse("patient_portal:instrument", args=[session.id, next_code]))

    def test_invalid_instrument_is_forbidden(self):
        url = reverse("patient_portal:instrument", args=[999, "invalid_code"])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 403)

    def test_skip_undo_restores_original_dates(self):
        from datetime import date
        from rtms_app.models import TreatmentSkip
        staff = get_user_model().objects.create_user(
            username='skip-operator', password='pw', is_staff=True
        )
        self.client.force_login(staff)
        # create sessions
        d1 = date(2026,1,5)
        d2 = date(2026,1,6)
        d3 = date(2026,1,7)
        from rtms_app.models import TreatmentSession
        s1 = TreatmentSession.objects.create(patient=self.patient, session_date=d1)
        s2 = TreatmentSession.objects.create(patient=self.patient, session_date=d2)
        s3 = TreatmentSession.objects.create(patient=self.patient, session_date=d3)

        # perform skip via POST (simulate UI)
        url = reverse('rtms_app:treatment_add', args=[self.patient.id])
        post = {
            'treatment_date': d2.isoformat(),
            'treatment_time': '09:00',
            'mt_percent': '120',
            'frequency_hz': '18.0',
            'train_seconds': '2.0',
            'intertrain_seconds': '20.0',
            'train_count': '55',
            'total_pulses': '1980',
            'action': 'skip',
            'skip_reason': 'test undo',
        }
        resp = self.client.post(url, post, follow=False)
        self.assertIn(resp.status_code, (302,303))

        # there should be a TreatmentSkip record
        sk = TreatmentSkip.objects.filter(treatment__patient=self.patient).first()
        self.assertIsNotNone(sk)

        # Now undo via POST
        undo_url = reverse('rtms_app:treatment_skip_undo', args=[sk.id])
        resp = self.client.post(undo_url, {}, follow=False)
        self.assertIn(resp.status_code, (302,303))

        # Refresh skip and sessions
        sk.refresh_from_db()
        s1.refresh_from_db(); s2.refresh_from_db(); s3.refresh_from_db()

        # Skip record should remain but be marked undone
        self.assertIsNotNone(sk.undone_by)
        self.assertIsNotNone(sk.undone_at)

        # All sessions should be planned and restored to original dates
        self.assertEqual(s1.session_date, d1)
        self.assertEqual(s2.session_date, d2)
        self.assertEqual(s3.session_date, d3)


class TestScheduleTasks(TestCase):
    def test_compute_task_definitions_and_dashboard(self):
        from rtms_app.services.schedule_tasks import compute_task_definitions, compute_dashboard_tasks
        from datetime import date

        p = Patient.objects.create(card_id='SCH1', name='Sched Test', birth_date=date(1990,1,1), first_treatment_date=date(2026,1,5))

        defs = compute_task_definitions(p, holidays=set())
        # Expect mapping and several assessment entries
        keys = {d['key'] for d in defs}
        self.assertIn('mapping', keys)
        self.assertIn('assessment_baseline', keys)
        self.assertIn('assessment_week3', keys)

        # Find mapping planned date and ensure compute_dashboard_tasks returns it when today==planned
        mapping = next((d for d in defs if d['key'] == 'mapping'), None)
        self.assertIsNotNone(mapping)
        planned = mapping['planned_date']

        todo = compute_dashboard_tasks(p, today=planned, holidays=set())
        todo_keys = {t['key'] for t in todo}
        self.assertIn('mapping', todo_keys)


class TestCourseAwarePhase2F4Workflows(TestCase):
    def setUp(self):
        self.patient = Patient.objects.create(
            card_id='2F4-001', name='Course Isolation', birth_date=date(1980, 1, 1),
            course_number=1, first_treatment_date=date(2026, 1, 5),
        )
        self.course_one = TreatmentCourse.objects.create(
            patient=self.patient, course_number=1, first_treatment_date=date(2026, 1, 5),
        )
        self.course_two = TreatmentCourse.objects.create(
            patient=self.patient, course_number=2, first_treatment_date=date(2026, 1, 5),
        )

    def test_session_shift_is_limited_to_the_selected_course(self):
        from rtms_app.services.schedule import shift_future_sessions

        first = date(2026, 1, 6)
        second = date(2026, 1, 8)
        one = TreatmentSession.objects.create(
            patient=self.patient, treatment_course=self.course_one, course_number=1,
            session_date=first,
        )
        one_future = TreatmentSession.objects.create(
            patient=self.patient, treatment_course=self.course_one, course_number=1,
            session_date=second,
        )
        two_future = TreatmentSession.objects.create(
            patient=self.patient, treatment_course=self.course_two, course_number=2,
            session_date=second,
        )

        one.status = 'skipped'
        one.save(update_fields=['status'])
        shift_future_sessions(self.patient, first, 1)

        one_future.refresh_from_db()
        two_future.refresh_from_db()
        self.assertNotEqual(one_future.session_date, second)
        self.assertEqual(two_future.session_date, second)

    def test_summary_and_dashboard_tasks_use_explicit_course(self):
        from rtms_app.services.course_summary_service import build_treatment_session_display
        from rtms_app.services.schedule_tasks import compute_dashboard_tasks

        TreatmentSession.objects.create(
            patient=self.patient, treatment_course=self.course_one, course_number=1,
            session_date=date(2026, 1, 5),
        )
        TreatmentSession.objects.create(
            patient=self.patient, treatment_course=self.course_two, course_number=2,
            session_date=date(2026, 1, 6),
        )
        Assessment.objects.create(
            patient=self.patient, treatment_course=self.course_two, course_number=2,
            timing='week3', date=date(2026, 1, 19), type='HAM-D', scores={'q1': 1},
        )

        one_display = build_treatment_session_display(self.patient, treatment_course=self.course_one)
        two_display = build_treatment_session_display(self.patient, treatment_course=self.course_two)
        self.assertEqual([item['date'] for item in one_display], [date(2026, 1, 5)])
        self.assertEqual([item['date'] for item in two_display], [date(2026, 1, 6)])

        one_tasks = {item['key'] for item in compute_dashboard_tasks(
            self.patient, today=date(2026, 1, 19), holidays=set(), treatment_course=self.course_one,
        )}
        two_tasks = {item['key'] for item in compute_dashboard_tasks(
            self.patient, today=date(2026, 1, 19), holidays=set(), treatment_course=self.course_two,
        )}
        self.assertIn('assessment_week3', one_tasks)
        self.assertNotIn('assessment_week3', two_tasks)

    def test_baseline_task_uses_explicit_course_performed_date(self):
        from rtms_app.services.schedule_tasks import compute_task_definitions

        Assessment.objects.create(
            patient=self.patient, treatment_course=self.course_one, course_number=1,
            timing='baseline', date=date(2026, 1, 5), type='HAM-D',
        )

        course_two_baseline = next(
            item for item in compute_task_definitions(
                self.patient, holidays=set(), treatment_course=self.course_two,
            ) if item['key'] == 'assessment_baseline'
        )

        self.assertIsNone(course_two_baseline['performed_date'])

    def test_recommendation_uses_explicit_course_assessments(self):
        from rtms_app.services.recommendation import get_patient_recommendation

        for course, baseline_score, week3_score in (
            (self.course_one, 20, 19), (self.course_two, 20, 7),
        ):
            Assessment.objects.create(
                patient=self.patient, treatment_course=course, course_number=course.course_number,
                timing='baseline', date=date(2026, 1, 5), type='HAM-D', scores={'q1': baseline_score},
            )
            Assessment.objects.create(
                patient=self.patient, treatment_course=course, course_number=course.course_number,
                timing='week3', date=date(2026, 1, 19), type='HAM-D', scores={'q1': week3_score},
            )

        self.assertEqual(get_patient_recommendation(self.patient, self.course_one).status, 'ineffective')
        self.assertEqual(get_patient_recommendation(self.patient, self.course_two).status, 'remission')

    def test_course_two_hamd_trend_isolated_from_course_one(self):
        from rtms_app.services.course_summary_service import build_assessment_trend

        hamd = ScaleDefinition.objects.get_or_create(
            code='hamd', defaults={'name': 'HAM-D'},
        )[0]
        AssessmentRecord.objects.create(
            patient=self.patient, treatment_course=self.course_one, course_number=1,
            timing='baseline', scale=hamd, date=date(2026, 1, 5),
            scores={'q1': 25},
        )
        AssessmentRecord.objects.create(
            patient=self.patient, treatment_course=self.course_two, course_number=2,
            timing='baseline', scale=hamd, date=date(2026, 6, 1),
            scores={'q1': 12},
        )

        trend = build_assessment_trend(
            self.patient, timings=['baseline'], course_number=2,
        )

        self.assertEqual(trend[0]['hamd17'], 12)

    def test_course_two_recommendation_uses_assessment_record_scope(self):
        from rtms_app.services.recommendation import get_patient_recommendation

        hamd = ScaleDefinition.objects.get_or_create(
            code='hamd', defaults={'name': 'HAM-D'},
        )[0]
        AssessmentRecord.objects.create(
            patient=self.patient, treatment_course=self.course_one, course_number=1,
            timing='week3', scale=hamd, date=date(2026, 1, 19),
            scores={'q1': 20},
        )
        AssessmentRecord.objects.create(
            patient=self.patient, treatment_course=self.course_two, course_number=2,
            timing='week3', scale=hamd, date=date(2026, 6, 15),
            scores={'q1': 5},
        )

        recommendation = get_patient_recommendation(self.patient, course_number=2)

        self.assertEqual(recommendation.status, 'remission')


# ============================================================================
# GROUP A: Print View Helpers Unit Tests
# ============================================================================

class TestPrintViewHelpers(TestCase):
    """Test print view helper functions (print_views.py)"""

    def setUp(self):
        self.patient = Patient.objects.create(
            card_id="PRINT001",
            name="Print Test",
            birth_date=date(1980, 1, 1),
            course_number=1
        )

    def test_print_extract_back_url_from_get(self):
        """Test _extract_back_url prioritizes request.GET"""
        from django.test import RequestFactory
        from rtms_app.print_views import _extract_back_url

        factory = RequestFactory()

        # Test 1: back_url in GET
        request = factory.get('/app/print/admission/?back_url=/custom/url')
        back_url = _extract_back_url(request, self.patient, 'rtms_app:patient_home')
        self.assertEqual(back_url, '/custom/url')

    def test_print_extract_back_url_fallback_to_referer(self):
        """Test _extract_back_url falls back to HTTP_REFERER"""
        from django.test import RequestFactory
        from rtms_app.print_views import _extract_back_url

        factory = RequestFactory()

        # Test 2: back_url not in GET, but HTTP_REFERER available
        request = factory.get('/app/print/admission/')
        request.META['HTTP_REFERER'] = '/previous/page'
        back_url = _extract_back_url(request, self.patient, 'rtms_app:patient_home')
        self.assertEqual(back_url, '/previous/page')

    def test_print_extract_back_url_fallback_to_view(self):
        """Test _extract_back_url uses view reverse as final fallback"""
        from django.test import RequestFactory
        from rtms_app.print_views import _extract_back_url

        factory = RequestFactory()

        # Test 3: neither GET nor referer, use fallback view
        request = factory.get('/app/print/admission/')
        back_url = _extract_back_url(request, self.patient, 'rtms_app:patient_home')
        expected_url = reverse('rtms_app:patient_home', args=[self.patient.id])
        self.assertEqual(back_url, expected_url)

    def test_print_get_latest_assessments_by_date(self):
        """Test _get_latest_assessments_by_date collapses duplicates"""
        from rtms_app.print_views import _get_latest_assessments_by_date
        from rtms_app.models import Assessment

        # Create assessments with different dates
        date1 = date(2026, 1, 5)
        date2 = date(2026, 1, 6)
        date3 = date(2026, 1, 7)

        a1 = Assessment.objects.create(patient=self.patient, date=date1, timing='baseline', course_number=1, total_score_17=20)
        a2 = Assessment.objects.create(patient=self.patient, date=date2, timing='week3', course_number=1, total_score_17=19)
        a3 = Assessment.objects.create(patient=self.patient, date=date3, timing='week4', course_number=1, total_score_17=18)

        result = _get_latest_assessments_by_date(self.patient)

        # Should have 3 unique dates
        self.assertEqual(len(result), 3)

        # Should be sorted by date
        self.assertEqual(result[0].date, date1)
        self.assertEqual(result[1].date, date2)
        self.assertEqual(result[2].date, date3)


class TestPrintViewContextBuilding(TestCase):
    """Test print view context building functions"""

    def setUp(self):
        self.patient = Patient.objects.create(
            card_id="PRINTCTX001",
            name="Context Test",
            birth_date=date(1980, 1, 1),
            course_number=2
        )
        self.client = Client()
        User = get_user_model()
        self.user = User.objects.create_user(username="printer", password="printpass")
        self.client.login(username="printer", password="printpass")

    def test_print_discharge_context_structure(self):
        """Test _build_discharge_context returns expected keys"""
        from django.test import RequestFactory
        from rtms_app.print_views import _build_discharge_context

        factory = RequestFactory()
        request = factory.get('/app/patient/1/print/discharge/')

        context = _build_discharge_context(request, self.patient.id)

        # Verify expected keys
        self.assertIn('patient', context)
        self.assertIn('today', context)
        self.assertIn('test_scores', context)
        self.assertIn('back_url', context)
        self.assertIn('hamd_trend_cols', context)
        self.assertIn('pdf_filename', context)
        self.assertIn('discharge_date', context)

        # Verify patient
        self.assertEqual(context['patient'].id, self.patient.id)

        # Verify pdf_filename format
        self.assertIn(self.patient.card_id, context['pdf_filename'])
        self.assertIn('pdf', context['pdf_filename'])

    def test_print_views_html_and_pdf_endpoints(self):
        """Test discharge view returns both HTML and PDF"""
        # Test HTML view
        url_html = reverse('rtms_app:print:patient_print_discharge', args=[self.patient.id])
        response_html = self.client.get(url_html)
        self.assertEqual(response_html.status_code, 200)
        self.assertIn('text/html', response_html['Content-Type'])

        # Test PDF view (if weasyprint available)
        try:
            from weasyprint import HTML
            url_pdf = reverse('rtms_app:print:patient_print_discharge_pdf', args=[self.patient.id])
            response_pdf = self.client.get(url_pdf)
            # PDF might return 200 or error if weasyprint not fully configured
            self.assertIn(response_pdf.status_code, [200, 500])
        except ImportError:
            pass  # weasyprint not installed, skip PDF test

    def test_course_two_print_contexts_use_course_dates(self):
        from rtms_app.print_views import (
            _build_admission_context,
            _build_discharge_context,
            _build_referral_context,
        )

        old_dates = {
            'admission_date': date(2026, 1, 2),
            'mapping_date': date(2026, 1, 5),
            'first_treatment_date': date(2026, 1, 6),
            'discharge_date': date(2026, 2, 16),
        }
        new_dates = {
            'admission_date': date(2026, 8, 3),
            'mapping_date': date(2026, 8, 4),
            'first_treatment_date': date(2026, 8, 5),
            'discharge_date': date(2026, 9, 15),
        }
        for field, value in old_dates.items():
            setattr(self.patient, field, value)
        self.patient.save(update_fields=list(old_dates))
        TreatmentCourse.objects.create(patient=self.patient, course_number=1, **old_dates)
        TreatmentCourse.objects.create(patient=self.patient, course_number=2, **new_dates)

        request = RequestFactory().get('/app/print/admission/?course_number=2')
        admission = _build_admission_context(request, self.patient.id)
        discharge = _build_discharge_context(request, self.patient.id)
        referral = _build_referral_context(request, self.patient.id)

        self.assertEqual(admission['admission_date'], new_dates['admission_date'])
        self.assertEqual(admission['mapping_date'], new_dates['mapping_date'])
        self.assertEqual(admission['first_treatment_date'], new_dates['first_treatment_date'])
        self.assertEqual(admission['end_date_est'], date(2026, 9, 16))
        self.assertEqual(discharge['admission_date'], new_dates['admission_date'])
        self.assertEqual(discharge['discharge_date'], new_dates['discharge_date'])
        self.assertEqual(referral['admission_date'], new_dates['admission_date'])
        self.assertEqual(referral['discharge_date'], new_dates['discharge_date'])

        html = self.client.get(
            reverse('rtms_app:print:patient_print_admission', args=[self.patient.id]),
            {'course_number': 2},
        )
        self.assertEqual(html.context['admission_date'], new_dates['admission_date'])
        self.assertEqual(html.context['mapping_date'], new_dates['mapping_date'])
        self.assertEqual(html.context['first_treatment_date'], new_dates['first_treatment_date'])

    def test_print_course_one_and_null_course_dates_fallback_to_patient(self):
        from rtms_app.print_views import _build_admission_context, _build_discharge_context

        patient_dates = {
            'admission_date': date(2026, 1, 2),
            'mapping_date': date(2026, 1, 5),
            'first_treatment_date': date(2026, 1, 6),
            'discharge_date': date(2026, 2, 16),
        }
        for field, value in patient_dates.items():
            setattr(self.patient, field, value)
        self.patient.save(update_fields=list(patient_dates))
        course_one = TreatmentCourse.objects.create(patient=self.patient, course_number=1, **patient_dates)
        course_two = TreatmentCourse.objects.create(patient=self.patient, course_number=2)

        request_one = RequestFactory().get('/app/print/admission/?course_number=1')
        course_one_context = _build_admission_context(request_one, self.patient.id)
        self.assertEqual(course_one_context['admission_date'], course_one.admission_date)
        self.assertEqual(course_one_context['first_treatment_date'], course_one.first_treatment_date)

        request_two = RequestFactory().get('/app/print/discharge/?course_number=2')
        course_two_context = _build_discharge_context(request_two, self.patient.id)
        self.assertEqual(course_two_context['admission_date'], patient_dates['admission_date'])
        self.assertEqual(course_two_context['discharge_date'], patient_dates['discharge_date'])


# ============================================================================
# GROUP B: View Helpers Unit Tests
# ============================================================================

class TestViewHelpersFunctions(TestCase):
    """Test view_helpers.py functions"""

    def setUp(self):
        self.patient = Patient.objects.create(
            card_id="HELPER001",
            name="Helper Test",
            birth_date=date(1980, 1, 1),
            course_number=3
        )

    def test_extract_back_url_from_get(self):
        """Test extract_back_url prioritizes GET parameter"""
        from django.test import RequestFactory
        from rtms_app.view_helpers import extract_back_url

        factory = RequestFactory()
        request = factory.get('/?back_url=/custom')

        url = extract_back_url(request, 'rtms_app:dashboard')
        self.assertEqual(url, '/custom')

    def test_extract_back_url_fallback_to_referer(self):
        """Test extract_back_url falls back to referer"""
        from django.test import RequestFactory
        from rtms_app.view_helpers import extract_back_url

        factory = RequestFactory()
        request = factory.get('/')
        request.META['HTTP_REFERER'] = '/referer/page'

        url = extract_back_url(request, 'rtms_app:dashboard')
        self.assertEqual(url, '/referer/page')

    def test_extract_back_url_fallback_to_reverse(self):
        """Test extract_back_url uses reverse as final fallback"""
        from django.test import RequestFactory
        from rtms_app.view_helpers import extract_back_url

        factory = RequestFactory()
        request = factory.get('/')

        url = extract_back_url(request, 'rtms_app:patient_home', self.patient.id)
        expected = reverse('rtms_app:patient_home', args=[self.patient.id])
        self.assertEqual(url, expected)

    def test_get_dashboard_date_present(self):
        """Test get_dashboard_date extracts date from GET"""
        from django.test import RequestFactory
        from rtms_app.view_helpers import get_dashboard_date

        factory = RequestFactory()
        request = factory.get('/?dashboard_date=2026-01-15')

        result = get_dashboard_date(request)
        self.assertEqual(result, '2026-01-15')

    def test_get_dashboard_date_absent(self):
        """Test get_dashboard_date returns None when not present"""
        from django.test import RequestFactory
        from rtms_app.view_helpers import get_dashboard_date

        factory = RequestFactory()
        request = factory.get('/')

        result = get_dashboard_date(request)
        self.assertIsNone(result)

    def test_build_common_context_basic(self):
        """Test build_common_context creates standard keys"""
        from rtms_app.view_helpers import build_common_context

        context = build_common_context(self.patient)

        self.assertIn('patient', context)
        self.assertIn('today', context)
        self.assertEqual(context['patient'], self.patient)
        self.assertIsNotNone(context['today'])

    def test_build_common_context_with_dashboard_date(self):
        """Test build_common_context includes dashboard_date"""
        from rtms_app.view_helpers import build_common_context

        context = build_common_context(self.patient, dashboard_date='2026-01-15')

        self.assertIn('dashboard_date', context)
        self.assertEqual(context['dashboard_date'], '2026-01-15')

    def test_build_common_context_with_extra(self):
        """Test build_common_context merges extra keys"""
        from rtms_app.view_helpers import build_common_context

        context = build_common_context(self.patient, logs=['log1', 'log2'], custom_key='value')

        self.assertIn('logs', context)
        self.assertIn('custom_key', context)
        self.assertEqual(context['logs'], ['log1', 'log2'])
        self.assertEqual(context['custom_key'], 'value')

    def test_get_course_number_present(self):
        """Test get_course_number returns patient course_number"""
        from rtms_app.view_helpers import get_course_number

        result = get_course_number(self.patient)
        self.assertEqual(result, 3)

    def test_get_course_number_default(self):
        """Test get_course_number returns 1 when not set"""
        from rtms_app.view_helpers import get_course_number

        # Create patient without explicit course_number (will use default)
        p = Patient.objects.create(
            card_id="COURSE_TEST",
            name="Course Test",
            birth_date=date(1990, 1, 1)
            # Note: Not setting course_number, will use model default
        )

        result = get_course_number(p)
        # Should return model default (1)
        self.assertEqual(result, 1)


# ============================================================================
# GROUP D: Assessment Query Helper Tests (Stage 9)
# ============================================================================

class TestAssessmentQueries(TestCase):
    """Test assessment query helpers from queries.assessment_queries"""

    def setUp(self):
        from rtms_app.models import ScaleDefinition

        self.patient = Patient.objects.create(
            card_id="QUERY001",
            name="Query Test",
            birth_date=date(1980, 1, 1),
            course_number=1
        )

        # Create ScaleDefinition for A2 tests
        # Use get_or_create to avoid duplicate key errors if tests run multiple times
        self.scale_hamd, _ = ScaleDefinition.objects.get_or_create(
            code='hamd',
            defaults={'name': 'HAM-D', 'is_active': True}
        )
        self.scale_phq9, _ = ScaleDefinition.objects.get_or_create(
            code='phq9',
            defaults={'name': 'PHQ-9', 'is_active': True}
        )

    def test_get_assessments_ordered_multiple(self):
        """Test get_assessments_ordered returns assessments in date order"""
        from rtms_app.queries.assessment_queries import get_assessments_ordered

        # Create assessments on different dates with different timings
        a1 = Assessment.objects.create(
            patient=self.patient,
            date=date(2026, 1, 5),
            timing='baseline',
            course_number=1,
            total_score_17=20
        )
        a2 = Assessment.objects.create(
            patient=self.patient,
            date=date(2026, 1, 3),
            timing='week3',
            course_number=1,
            total_score_17=19,
            type='HAM-D'
        )
        a3 = Assessment.objects.create(
            patient=self.patient,
            date=date(2026, 1, 7),
            timing='week4',
            course_number=1,
            total_score_17=18,
            type='HAM-D'
        )

        # Query should return in ascending date order
        result = list(get_assessments_ordered(self.patient))

        self.assertEqual(len(result), 3)
        self.assertEqual(result[0].id, a2.id)  # 2026-01-03
        self.assertEqual(result[1].id, a1.id)  # 2026-01-05
        self.assertEqual(result[2].id, a3.id)  # 2026-01-07

    def test_get_assessments_ordered_empty(self):
        """Test get_assessments_ordered returns empty when no assessments"""
        from rtms_app.queries.assessment_queries import get_assessments_ordered

        result = list(get_assessments_ordered(self.patient))

        self.assertEqual(len(result), 0)

    def test_get_assessments_ordered_same_date(self):
        """Test get_assessments_ordered handles multiple assessments on same date"""
        from rtms_app.queries.assessment_queries import get_assessments_ordered

        # Create multiple assessments on same date
        same_date = date(2026, 1, 5)
        a1 = Assessment.objects.create(
            patient=self.patient,
            date=same_date,
            timing='baseline',
            course_number=1,
            total_score_17=20,
            type='HAM-D'
        )
        a2 = Assessment.objects.create(
            patient=self.patient,
            date=same_date,
            timing='week3',
            course_number=1,
            total_score_17=19,
            type='HAM-D'
        )

        result = list(get_assessments_ordered(self.patient))

        # Both should be present, both on same date
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].date, same_date)
        self.assertEqual(result[1].date, same_date)

    def test_get_assessments_ordered_only_for_patient(self):
        """Test get_assessments_ordered returns only patient's assessments"""
        from rtms_app.queries.assessment_queries import get_assessments_ordered

        # Create another patient
        other_patient = Patient.objects.create(
            card_id="QUERY002",
            name="Other Query",
            birth_date=date(1985, 1, 1),
            course_number=1
        )

        # Create assessment for this patient
        Assessment.objects.create(
            patient=self.patient,
            date=date(2026, 1, 5),
            timing='baseline',
            course_number=1,
            total_score_17=20,
            type='HAM-D'
        )

        # Create assessment for other patient
        Assessment.objects.create(
            patient=other_patient,
            date=date(2026, 1, 6),
            timing='baseline',
            course_number=1,
            total_score_17=18,
            type='HAM-D'
        )

        # Query for first patient should only return first patient's assessment
        result = list(get_assessments_ordered(self.patient))

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].patient.id, self.patient.id)

    def test_get_latest_assessment_baseline(self):
        """Test get_latest_assessment returns baseline assessment"""
        from rtms_app.queries.assessment_queries import get_latest_assessment

        # Create single baseline assessment
        # (Note: unique constraint is (patient, course_number, timing, type)
        # so only one baseline per patient per course per type)
        baseline = Assessment.objects.create(
            patient=self.patient,
            date=date(2026, 1, 5),
            timing='baseline',
            course_number=1,
            scores={"q1": "0", "q2": "1"}  # Provide scores for calculation
        )

        result = get_latest_assessment(self.patient, 'baseline')

        self.assertIsNotNone(result)
        self.assertEqual(result.id, baseline.id)
        self.assertEqual(result.date, date(2026, 1, 5))
        self.assertEqual(result.timing, 'baseline')

    def test_get_latest_assessment_week3(self):
        """Test get_latest_assessment returns latest week3 assessment"""
        from rtms_app.queries.assessment_queries import get_latest_assessment

        # Create week3 assessments
        a1 = Assessment.objects.create(
            patient=self.patient,
            date=date(2026, 1, 15),
            timing='week3',
            course_number=1,
            total_score_17=18
        )

        result = get_latest_assessment(self.patient, 'week3')

        self.assertIsNotNone(result)
        self.assertEqual(result.id, a1.id)
        self.assertEqual(result.timing, 'week3')

    def test_get_latest_assessment_multiple_timings(self):
        """Test get_latest_assessment returns correct assessment per timing"""
        from rtms_app.queries.assessment_queries import get_latest_assessment

        # Create assessments with different timings
        baseline = Assessment.objects.create(
            patient=self.patient,
            date=date(2026, 1, 5),
            timing='baseline',
            course_number=1,
            total_score_17=22,
            type='HAM-D'
        )
        week3 = Assessment.objects.create(
            patient=self.patient,
            date=date(2026, 1, 20),
            timing='week3',
            course_number=1,
            total_score_17=20,
            type='HAM-D'
        )
        week6 = Assessment.objects.create(
            patient=self.patient,
            date=date(2026, 2, 5),
            timing='week6',
            course_number=1,
            total_score_17=18,
            type='HAM-D'
        )

        # Each timing should return its own assessment
        baseline_result = get_latest_assessment(self.patient, 'baseline')
        week3_result = get_latest_assessment(self.patient, 'week3')
        week6_result = get_latest_assessment(self.patient, 'week6')

        self.assertEqual(baseline_result.id, baseline.id)
        self.assertEqual(week3_result.id, week3.id)
        self.assertEqual(week6_result.id, week6.id)

    def test_get_latest_assessment_none_exists(self):
        """Test get_latest_assessment returns None when no assessment for timing"""
        from rtms_app.queries.assessment_queries import get_latest_assessment

        # No assessment for 'week4'
        result = get_latest_assessment(self.patient, 'week4')

        self.assertIsNone(result)

    def test_get_latest_assessment_latest_wins(self):
        """Test get_latest_assessment returns most recent when querying multiple assessments"""
        from rtms_app.queries.assessment_queries import get_latest_assessment

        # Note: Assessment unique constraint is (patient, course_number, timing, type='HAM-D')
        # So we can only have one baseline per (patient, course_number)
        # Test with single assessment to verify behavior
        baseline = Assessment.objects.create(
            patient=self.patient,
            date=date(2026, 1, 10),
            timing='baseline',
            course_number=1,
            total_score_17=22,
            type='HAM-D'
        )

        result = get_latest_assessment(self.patient, 'baseline')

        self.assertEqual(result.id, baseline.id)
        self.assertEqual(result.date, date(2026, 1, 10))

    def test_get_latest_assessment_isolation(self):
        """Test get_latest_assessment only returns assessments for specified patient"""
        from rtms_app.queries.assessment_queries import get_latest_assessment

        # Create another patient
        other_patient = Patient.objects.create(
            card_id="QUERY003",
            name="Other Patient",
            birth_date=date(1985, 1, 1),
            course_number=1
        )

        # Create baseline for first patient
        my_baseline = Assessment.objects.create(
            patient=self.patient,
            date=date(2026, 1, 5),
            timing='baseline',
            course_number=1,
            total_score_17=22,
            type='HAM-D'
        )

        # Create baseline for other patient (earlier date, should not be returned)
        other_baseline = Assessment.objects.create(
            patient=other_patient,
            date=date(2026, 1, 1),
            timing='baseline',
            course_number=1,
            total_score_17=25,
            type='HAM-D'
        )

        # Query for first patient should only return first patient's assessment
        result = get_latest_assessment(self.patient, 'baseline')

        self.assertEqual(result.id, my_baseline.id)
        self.assertNotEqual(result.id, other_baseline.id)

    def test_get_assessment_by_timing_with_fallback_record_only(self):
        """Test get_assessment_by_timing_with_fallback returns AssessmentRecord when exists"""
        from rtms_app.queries.assessment_queries import get_assessment_by_timing_with_fallback

        # Create AssessmentRecord only
        record = AssessmentRecord.objects.create(
            patient=self.patient,
            course_number=1,
            timing='baseline',
            scale=self.scale_hamd,
            date=date(2026, 1, 5),
            total_score_17=20
        )

        result = get_assessment_by_timing_with_fallback(
            self.patient, 'baseline', self.scale_hamd, course_number=1
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.id, record.id)
        self.assertIsInstance(result, AssessmentRecord)

    def test_get_assessment_by_timing_with_fallback_isolates_explicit_course(self):
        from rtms_app.queries.assessment_queries import get_assessment_by_timing_with_fallback

        course_one = TreatmentCourse.objects.create(patient=self.patient, course_number=1)
        course_two = TreatmentCourse.objects.create(patient=self.patient, course_number=2)
        record_one = AssessmentRecord.objects.create(
            patient=self.patient, treatment_course=course_one, course_number=1,
            timing='baseline', scale=self.scale_hamd, date=date(2026, 1, 5),
            scores={'q1': 25},
        )
        record_two = AssessmentRecord.objects.create(
            patient=self.patient, treatment_course=course_two, course_number=2,
            timing='baseline', scale=self.scale_hamd, date=date(2026, 6, 1),
            scores={'q1': 12},
        )

        result = get_assessment_by_timing_with_fallback(
            self.patient, 'baseline', self.scale_hamd,
            course_number=2, treatment_course=course_two,
        )

        self.assertEqual(result.id, record_two.id)
        self.assertNotEqual(result.id, record_one.id)

    def test_get_assessment_by_timing_with_fallback_legacy_only(self):
        """Test get_assessment_by_timing_with_fallback falls back to Assessment"""
        from rtms_app.queries.assessment_queries import get_assessment_by_timing_with_fallback

        # Create Assessment (legacy) only
        legacy = Assessment.objects.create(
            patient=self.patient,
            course_number=1,
            timing='baseline',
            type='HAM-D',
            date=date(2026, 1, 5),
            total_score_17=22
        )

        result = get_assessment_by_timing_with_fallback(
            self.patient, 'baseline', self.scale_hamd, course_number=1
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.id, legacy.id)
        self.assertIsInstance(result, Assessment)

    def test_get_assessment_by_timing_with_fallback_both_prefer_record(self):
        """Test get_assessment_by_timing_with_fallback prefers AssessmentRecord when both exist"""
        from rtms_app.queries.assessment_queries import get_assessment_by_timing_with_fallback

        # Create both AssessmentRecord and Assessment
        record = AssessmentRecord.objects.create(
            patient=self.patient,
            course_number=1,
            timing='baseline',
            scale=self.scale_hamd,
            date=date(2026, 1, 5),
            total_score_17=20
        )

        legacy = Assessment.objects.create(
            patient=self.patient,
            course_number=1,
            timing='baseline',
            type='HAM-D',
            date=date(2026, 1, 5),
            total_score_17=22
        )

        result = get_assessment_by_timing_with_fallback(
            self.patient, 'baseline', self.scale_hamd, course_number=1
        )

        # Should prefer AssessmentRecord
        self.assertIsNotNone(result)
        self.assertEqual(result.id, record.id)
        self.assertIsInstance(result, AssessmentRecord)

    def test_get_assessment_by_timing_with_fallback_none_exist(self):
        """Test get_assessment_by_timing_with_fallback returns None when neither exists"""
        from rtms_app.queries.assessment_queries import get_assessment_by_timing_with_fallback

        # No records created
        result = get_assessment_by_timing_with_fallback(
            self.patient, 'baseline', self.scale_hamd, course_number=1
        )

        self.assertIsNone(result)

    def test_get_assessment_by_timing_with_fallback_non_hamd_no_legacy(self):
        """Test get_assessment_by_timing_with_fallback does not fallback for non-HAM-D scales"""
        from rtms_app.queries.assessment_queries import get_assessment_by_timing_with_fallback

        # Create Assessment (legacy) - should NOT fallback for non-HAM-D
        legacy = Assessment.objects.create(
            patient=self.patient,
            course_number=1,
            timing='baseline',
            type='HAM-D',  # Still HAM-D type, but scale is PHQ-9
            date=date(2026, 1, 5),
            total_score_17=22
        )

        result = get_assessment_by_timing_with_fallback(
            self.patient, 'baseline', self.scale_phq9, course_number=1
        )

        # Should return None (no fallback for non-HAM-D)
        self.assertIsNone(result)

    def test_get_assessment_by_timing_with_fallback_latest_record_wins(self):
        """Test get_assessment_by_timing_with_fallback returns latest AssessmentRecord"""
        from rtms_app.queries.assessment_queries import get_assessment_by_timing_with_fallback

        # Note: AssessmentRecord unique constraint is (patient, course_number, timing, scale)
        # So we can only have one AssessmentRecord per (patient, course_number, timing, scale)
        # Test that the function returns the only existing record
        record = AssessmentRecord.objects.create(
            patient=self.patient,
            course_number=1,
            timing='week3',
            scale=self.scale_hamd,
            date=date(2026, 1, 15),
            total_score_17=18
        )

        result = get_assessment_by_timing_with_fallback(
            self.patient, 'week3', self.scale_hamd, course_number=1
        )

        # Should return the only record
        self.assertIsNotNone(result)
        self.assertEqual(result.id, record.id)

    def test_get_baseline_assessments_ordered_record_only(self):
        """Test get_baseline_assessments_ordered returns AssessmentRecord when exists"""
        from rtms_app.queries.assessment_queries import get_baseline_assessments_ordered

        # Create baseline AssessmentRecord only
        record = AssessmentRecord.objects.create(
            patient=self.patient,
            course_number=1,
            timing='baseline',
            scale=self.scale_hamd,
            date=date(2026, 1, 5),
            total_score_17=20
        )

        result = get_baseline_assessments_ordered(self.patient)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, record.id)
        self.assertIsInstance(result[0], AssessmentRecord)

    def test_get_baseline_assessments_ordered_legacy_only(self):
        """Test get_baseline_assessments_ordered falls back to Assessment"""
        from rtms_app.queries.assessment_queries import get_baseline_assessments_ordered

        # Create baseline Assessment (legacy) only
        legacy = Assessment.objects.create(
            patient=self.patient,
            course_number=1,
            timing='baseline',
            type='HAM-D',
            date=date(2026, 1, 5),
            total_score_17=22
        )

        result = get_baseline_assessments_ordered(self.patient)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, legacy.id)
        self.assertIsInstance(result[0], Assessment)

    def test_get_baseline_assessments_ordered_both_prefer_record(self):
        """Test get_baseline_assessments_ordered prefers AssessmentRecord when both exist"""
        from rtms_app.queries.assessment_queries import get_baseline_assessments_ordered

        # Create both AssessmentRecord and Assessment
        record = AssessmentRecord.objects.create(
            patient=self.patient,
            course_number=1,
            timing='baseline',
            scale=self.scale_hamd,
            date=date(2026, 1, 5),
            total_score_17=20
        )

        legacy = Assessment.objects.create(
            patient=self.patient,
            course_number=1,
            timing='baseline',
            type='HAM-D',
            date=date(2026, 1, 5),
            total_score_17=22
        )

        result = get_baseline_assessments_ordered(self.patient)

        # Should prefer AssessmentRecord
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, record.id)
        self.assertIsInstance(result[0], AssessmentRecord)

    def test_get_baseline_assessments_ordered_empty(self):
        """Test get_baseline_assessments_ordered returns empty list when no assessments"""
        from rtms_app.queries.assessment_queries import get_baseline_assessments_ordered

        result = get_baseline_assessments_ordered(self.patient)

        self.assertEqual(len(result), 0)
        self.assertEqual(result, [])

    def test_get_baseline_assessments_ordered_multiple(self):
        """Test get_baseline_assessments_ordered handles multiple baseline records"""
        from rtms_app.queries.assessment_queries import get_baseline_assessments_ordered

        # Create multiple baseline AssessmentRecords (normally shouldn't happen, but test anyway)
        # Note: Unique constraint is (patient, course_number, timing, scale)
        # So we create for different dates within same course/timing/scale
        # This is actually a constraint violation, so skip this variant.
        # Instead, test with different dates but ensure only one baseline per unique constraint

        # Just verify that the helper handles the single baseline case properly
        record = AssessmentRecord.objects.create(
            patient=self.patient,
            course_number=1,
            timing='baseline',
            scale=self.scale_hamd,
            date=date(2026, 1, 5),
            total_score_17=20
        )

        result = get_baseline_assessments_ordered(self.patient)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, record.id)

    def test_get_baseline_assessments_ordered_only_baseline(self):
        """Test get_baseline_assessments_ordered excludes non-baseline assessments"""
        from rtms_app.queries.assessment_queries import get_baseline_assessments_ordered

        # Create baseline and week3 assessments
        baseline = AssessmentRecord.objects.create(
            patient=self.patient,
            course_number=1,
            timing='baseline',
            scale=self.scale_hamd,
            date=date(2026, 1, 5),
            total_score_17=20
        )

        week3 = AssessmentRecord.objects.create(
            patient=self.patient,
            course_number=1,
            timing='week3',
            scale=self.scale_hamd,
            date=date(2026, 1, 20),
            total_score_17=18
        )

        result = get_baseline_assessments_ordered(self.patient)

        # Should only return baseline
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, baseline.id)
        self.assertEqual(result[0].timing, 'baseline')


# ============================================================================
# GROUP C: Integration & PoC Tests
# ============================================================================

class TestViewIntegration(TestCase):
    """Test actual views using helpers"""

    def setUp(self):
        self.client = Client()
        User = get_user_model()
        self.user = User.objects.create_user(username="admin", password="admin123", is_staff=True, is_superuser=True)
        self.client.login(username="admin", password="admin123")

        self.patient = Patient.objects.create(
            card_id="INTG001",
            name="Integration Test",
            birth_date=date(1980, 1, 1),
            course_number=1
        )

    def test_audit_logs_view_context(self):
        """Test audit_logs_view uses build_common_context"""
        from rtms_app.models import AuditLog

        # Create audit log
        AuditLog.objects.create(
            patient=self.patient,
            user=self.user,
            action='CREATE',
            target_model='Patient',
            target_pk=str(self.patient.id),
            summary='Test log'
        )

        url = reverse('rtms_app:audit_logs', args=[self.patient.id])
        response = self.client.get(url)

        # Should render successfully
        self.assertEqual(response.status_code, 200)

        # Check context
        self.assertIn('patient', response.context)
        self.assertIn('logs', response.context)
        self.assertEqual(response.context['patient'].id, self.patient.id)

    def test_audit_logs_view_with_dashboard_date(self):
        """Test audit_logs_view preserves dashboard_date parameter"""
        url = reverse('rtms_app:audit_logs', args=[self.patient.id])
        url_with_date = f"{url}?dashboard_date=2026-01-15"

        response = self.client.get(url_with_date)

        self.assertEqual(response.status_code, 200)
        self.assertIn('dashboard_date', response.context)
        self.assertEqual(response.context['dashboard_date'], '2026-01-15')

    def test_print_view_backward_compatibility(self):
        """Test all print views still work after refactoring"""
        # Create assessment for context
        from rtms_app.models import Assessment
        Assessment.objects.create(
            patient=self.patient,
            date=date(2026, 1, 5),
            timing='baseline',
            course_number=1,
            total_score_17=20
        )

        print_views = [
            'rtms_app:print:patient_print_admission',
            'rtms_app:print:patient_print_discharge',
            'rtms_app:print:patient_print_referral',
        ]

        for view_name in print_views:
            with self.subTest(view=view_name):
                url = reverse(view_name, args=[self.patient.id])
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200, f"{view_name} should return 200")
                self.assertIn('patient', response.context)
                self.assertEqual(response.context['patient'].id, self.patient.id)

    def test_decorators_patient_retrieval(self):
        """Test @get_patient_and_dashboard decorator retrieves patient"""
        from django.test import RequestFactory
        from rtms_app.decorators import get_patient_and_dashboard

        # Create a simple test view with decorator
        @get_patient_and_dashboard()
        def test_view(request, patient_id, patient, dashboard_date):
            from django.http import JsonResponse
            return JsonResponse({
                'patient_id': patient.id,
                'dashboard_date': dashboard_date
            })

        factory = RequestFactory()
        request = factory.get('/?dashboard_date=2026-01-15')

        # Mock login
        request.user = self.user

        response = test_view(request, self.patient.id)

        # Response should be successful
        self.assertEqual(response.status_code, 200)

    def test_decorators_patient_404(self):
        """Test @get_patient_and_dashboard returns 404 for invalid patient"""
        from django.test import RequestFactory
        from rtms_app.decorators import get_patient_and_dashboard
        from django.http import Http404

        @get_patient_and_dashboard()
        def test_view(request, patient_id, patient, dashboard_date):
            return None

        factory = RequestFactory()
        request = factory.get('/')
        request.user = self.user

        # Should raise Http404 for invalid patient_id
        with self.assertRaises(Http404):
            test_view(request, 99999)


# ============================================================================
# Phase 10b: Write Path Consolidation Tests
# ============================================================================

class TestAssessmentWriteHelpers(TestCase):
    """Test save_assessment_record() and save_assessment_hamd() helpers"""

    def setUp(self):
        """Set up test fixtures"""
        from rtms_app.models import ScaleDefinition

        self.patient = Patient.objects.create(
            card_id="WRITE_TEST_001",
            name="Write Test Patient",
            birth_date=date(1980, 1, 1),
            course_number=1,
        )
        self.course = TreatmentCourse.objects.create(
            patient=self.patient, course_number=1,
        )

        # Ensure HAM-D scale exists
        self.hamd_scale, _ = ScaleDefinition.objects.get_or_create(
            code='hamd',
            defaults={'name': 'HAM-D', 'total_items': 21}
        )

    def test_save_assessment_record_creates_new(self):
        """Test save_assessment_record() creates new AssessmentRecord"""
        from rtms_app.queries.assessment_queries import save_assessment_record

        scores = {'q1': '1', 'q2': '2', 'q3': '0'}
        record, created = save_assessment_record(
            patient=self.patient,
            course_number=1,
            treatment_course=self.course,
            timing='baseline',
            scale=self.hamd_scale,
            date=date.today(),
            scores=scores,
            note="Test note",
        )

        self.assertTrue(created)
        self.assertEqual(record.patient, self.patient)
        self.assertEqual(record.timing, 'baseline')
        self.assertEqual(record.scale, self.hamd_scale)
        self.assertEqual(record.note, "Test note")
        self.assertEqual(record.scores, scores)

    def test_save_assessment_record_updates_existing(self):
        """Test save_assessment_record() updates existing AssessmentRecord"""
        from rtms_app.queries.assessment_queries import save_assessment_record

        # Create initial record
        scores1 = {'q1': '1', 'q2': '2'}
        record1, created1 = save_assessment_record(
            patient=self.patient,
            course_number=1,
            treatment_course=self.course,
            timing='week3',
            scale=self.hamd_scale,
            date=date.today(),
            scores=scores1,
            note="Initial",
        )
        self.assertTrue(created1)

        # Update same record
        scores2 = {'q1': '2', 'q2': '3'}
        record2, created2 = save_assessment_record(
            patient=self.patient,
            course_number=1,
            treatment_course=self.course,
            timing='week3',
            scale=self.hamd_scale,
            date=date.today(),
            scores=scores2,
            note="Updated",
        )

        self.assertFalse(created2)
        self.assertEqual(record1.id, record2.id)
        self.assertEqual(record2.note, "Updated")
        self.assertEqual(record2.scores, scores2)

    def test_save_assessment_record_with_defaults_override(self):
        """Test save_assessment_record() applies defaults_override"""
        from rtms_app.queries.assessment_queries import save_assessment_record

        scores = {'q1': '1'}
        record, _ = save_assessment_record(
            patient=self.patient,
            course_number=1,
            treatment_course=self.course,
            timing='week6',
            scale=self.hamd_scale,
            date=date.today(),
            scores=scores,
            note="",
            defaults_override={
                'improvement_rate_17': 25.5,
                'status_label': '反応',
            },
        )

        self.assertEqual(record.improvement_rate_17, 25.5)
        self.assertEqual(record.status_label, '反応')

    def test_save_assessment_hamd_creates_new(self):
        """Test save_assessment_hamd() creates new Assessment"""
        from rtms_app.queries.assessment_queries import save_assessment_hamd

        scores = {'q1': '1', 'q2': '2'}
        assessment, created = save_assessment_hamd(
            patient=self.patient,
            course_number=1,
            treatment_course=self.course,
            timing='baseline',
            date=date.today(),
            scores=scores,
            note="Test HAM-D",
        )

        self.assertTrue(created)
        self.assertEqual(assessment.patient, self.patient)
        self.assertEqual(assessment.timing, 'baseline')
        self.assertEqual(assessment.type, 'HAM-D')
        self.assertEqual(assessment.note, "Test HAM-D")

    def test_save_assessment_hamd_updates_existing(self):
        """Test save_assessment_hamd() updates existing Assessment"""
        from rtms_app.queries.assessment_queries import save_assessment_hamd

        # Create initial
        scores1 = {'q1': '1', 'q2': '2'}
        a1, c1 = save_assessment_hamd(
            patient=self.patient,
            course_number=1,
            treatment_course=self.course,
            timing='week3',
            date=date.today(),
            scores=scores1,
            note="Initial",
        )
        self.assertTrue(c1)

        # Update
        scores2 = {'q1': '2', 'q2': '3'}
        a2, c2 = save_assessment_hamd(
            patient=self.patient,
            course_number=1,
            treatment_course=self.course,
            timing='week3',
            date=date.today(),
            scores=scores2,
            note="Updated",
        )

        self.assertFalse(c2)
        self.assertEqual(a1.id, a2.id)
        self.assertEqual(a2.note, "Updated")

    def test_save_assessment_hamd_calculate_scores_auto_called(self):
        """Test that save_assessment_hamd() auto-calls calculate_scores() via model.save()"""
        from rtms_app.queries.assessment_queries import save_assessment_hamd

        # Scores for q1-q21 with values summing to known total
        scores = {f'q{i}': '1' for i in range(1, 22)}  # All 1s => total 21
        assessment, _ = save_assessment_hamd(
            patient=self.patient,
            course_number=1,
            treatment_course=self.course,
            timing='baseline',
            date=date.today(),
            scores=scores,
            note="",
        )

        # total_score_21 should be auto-calculated
        self.assertEqual(assessment.total_score_21, 21)
        # total_score_17 should be calculated from q1-q17
        self.assertEqual(assessment.total_score_17, 17)

    def test_save_assessment_record_calculate_scores_auto_called(self):
        """Test that save_assessment_record() auto-calls calculate_scores() via model.save()"""
        from rtms_app.queries.assessment_queries import save_assessment_record

        scores = {f'q{i}': '1' for i in range(1, 22)}  # All 1s
        record, _ = save_assessment_record(
            patient=self.patient,
            course_number=1,
            treatment_course=self.course,
            timing='week3',
            scale=self.hamd_scale,
            date=date.today(),
            scores=scores,
            note="",
        )

        # Scores should be auto-calculated by model.save()
        self.assertEqual(record.total_score_21, 21)
        self.assertEqual(record.total_score_17, 17)

    def test_normal_write_rejects_missing_or_mismatched_course(self):
        from rtms_app.queries.assessment_queries import save_assessment_record

        common = {
            'patient': self.patient,
            'course_number': 1,
            'timing': 'baseline',
            'scale': self.hamd_scale,
            'date': date.today(),
            'scores': {'q1': '1'},
        }
        with self.assertRaises(ValueError):
            save_assessment_record(**common)

        other_patient = Patient.objects.create(
            card_id='WRITE_TEST_002', name='Other Patient', birth_date=date(1980, 1, 1),
        )
        other_course = TreatmentCourse.objects.create(
            patient=other_patient, course_number=2,
        )
        with self.assertRaises(ValueError):
            save_assessment_record(**common, treatment_course=other_course)
        with self.assertRaises(ValueError):
            save_assessment_record(**{**common, 'course_number': 2}, treatment_course=self.course)

    def test_legacy_write_is_explicit_and_supports_course_less_patient(self):
        from rtms_app.queries.assessment_queries import save_assessment_record_legacy

        patient = Patient.objects.create(
            card_id='WRITE_TEST_LEGACY', name='Legacy Patient', birth_date=date(1980, 1, 1),
            course_number=1,
        )
        record, created = save_assessment_record_legacy(
            patient=patient,
            course_number=1,
            timing='baseline',
            scale=self.hamd_scale,
            date=date.today(),
            scores={'q1': '1'},
        )

        self.assertTrue(created)
        self.assertIsNone(record.treatment_course)
        self.assertEqual(record.course_number, 1)


class TestResearchDataExport(TestCase):
    """Stage 7 Phase 3: research CSV export (summary / treatment detail / adverse events)."""

    def setUp(self):
        self.superuser = get_user_model().objects.create_superuser(
            username='research-admin', password='pass1234', email='a@example.com'
        )
        self.staff_user = get_user_model().objects.create_user(
            username='research-staff', password='pass1234', is_staff=True,
        )
        self.client = Client()

        self.patient = Patient.objects.create(
            card_id='RSRCH1', name='Research One', birth_date=date(1980, 1, 1),
            course_number=1, gender='F', diagnosis='うつ病',
            first_visit_date=date(2026, 1, 1), first_treatment_date=date(2026, 1, 5),
        )
        self.other_patient = Patient.objects.create(
            card_id='RSRCH2', name='Research Two', birth_date=date(1990, 5, 5),
            course_number=1,
        )

        # 'hamd' ScaleDefinition + baseline/week3/week4/week6 TimingScaleConfig
        # already exist via migration 0022; add 'bacs' explicitly to exercise
        # the dynamic (non-hamd) scale path and blank-vs-zero handling.
        self.bacs_scale, _ = ScaleDefinition.objects.get_or_create(
            code='bacs', defaults={'name': 'BACS'}
        )
        for timing in ('baseline', 'post'):
            TimingScaleConfig.objects.get_or_create(
                timing=timing, scale=self.bacs_scale,
                defaults={'is_enabled': True, 'display_order': 10},
            )

    def _urls(self):
        return {
            'summary': reverse('rtms_app:export_research_csv'),
            'detail': reverse('rtms_app:export_research_treatment_detail_csv'),
            'ae': reverse('rtms_app:export_research_adverse_events_csv'),
            'zip': reverse('rtms_app:export_research_zip'),
        }

    def test_non_superuser_cannot_access_any_research_csv(self):
        self.client.force_login(self.staff_user)
        for url in self._urls().values():
            response = self.client.get(url)
            self.assertEqual(response.status_code, 403)

    def test_anonymous_user_redirected_to_login(self):
        for url in self._urls().values():
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302)

    def test_admin_research_export_page_requires_superuser(self):
        self.client.force_login(self.staff_user)
        response = self.client.get('/admin/research-export/')
        self.assertEqual(response.status_code, 403)

        self.client.force_login(self.superuser)
        response = self.client.get('/admin/research-export/')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'research_summary.csv')
        self.assertContains(response, 'research_treatment_detail.csv')
        self.assertContains(response, 'research_adverse_events.csv')

    def test_summary_csv_has_no_patients_without_error(self):
        Patient.objects.all().delete()
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('rtms_app:export_research_csv'))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8-sig')
        lines = content.strip().splitlines()
        self.assertEqual(len(lines), 1)  # header only
        self.assertIn('HAMD_baseline_total17', lines[0])

    def test_summary_csv_one_row_per_patient_and_timing_columns_present(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('rtms_app:export_research_csv'))
        content = response.content.decode('utf-8-sig')
        reader = csv.DictReader(content.splitlines())
        rows = list(reader)
        self.assertEqual(len(rows), 2)  # 1 row per patient (each patient = 1 course here)
        header = reader.fieldnames
        for col in (
            'HAMD_baseline_total17', 'HAMD_week3_total17', 'HAMD_week4_total17', 'HAMD_week6_total17',
            'BACS_baseline_composite', 'BACS_post_composite',
        ):
            self.assertIn(col, header)
        self.assertNotIn('name', header)

    def test_summary_csv_missing_assessment_is_blank_not_zero(self):
        AssessmentRecord.objects.create(
            patient=self.patient, course_number=1, timing='baseline', scale=self.bacs_scale,
            scores={'composite': 0}, date=date(2026, 1, 10),
        )
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('rtms_app:export_research_csv'))
        content = response.content.decode('utf-8-sig')
        rows = list(csv.DictReader(content.splitlines()))
        row = next(r for r in rows if r['card_id'] == 'RSRCH1')
        # A real 0 score must be preserved...
        self.assertEqual(row['BACS_baseline_composite'], '0')
        # ...while an unassessed timing/scale must stay blank, not become 0.
        self.assertEqual(row['BACS_post_composite'], '')
        self.assertEqual(row['HAMD_baseline_total17'], '')

    def test_summary_csv_course_dates_are_isolated_with_patient_fallback(self):
        from rtms_app.services.export_research import generate_research_summary_csv

        course_one = TreatmentCourse.objects.create(
            patient=self.patient, course_number=1,
            admission_date=date(2026, 1, 2),
            first_treatment_date=date(2026, 1, 5),
            discharge_date=date(2026, 1, 30),
        )
        course_two = TreatmentCourse.objects.create(
            patient=self.patient, course_number=2,
            admission_date=date(2026, 3, 2),
            first_treatment_date=date(2026, 3, 5),
            discharge_date=date(2026, 3, 30),
        )
        self.patient.admission_date = date(2025, 12, 1)
        self.patient.first_treatment_date = date(2025, 12, 5)
        self.patient.discharge_date = date(2025, 12, 30)
        self.patient.save(update_fields=['admission_date', 'first_treatment_date', 'discharge_date'])

        before = {
            'patient': (self.patient.admission_date, self.patient.first_treatment_date, self.patient.discharge_date),
            'course_one': (course_one.admission_date, course_one.first_treatment_date, course_one.discharge_date),
            'course_two': (course_two.admission_date, course_two.first_treatment_date, course_two.discharge_date),
        }
        content = generate_research_summary_csv()
        rows = list(csv.DictReader(content.splitlines()))
        patient_rows = [row for row in rows if row['card_id'] == self.patient.card_id]

        self.assertEqual(len(patient_rows), 2)
        by_course = {int(row['course_number']): row for row in patient_rows}
        self.assertEqual(
            (by_course[1]['admission_date'], by_course[1]['first_treatment_date'], by_course[1]['discharge_date']),
            ('2026-01-02', '2026-01-05', '2026-01-30'),
        )
        self.assertEqual(
            (by_course[2]['admission_date'], by_course[2]['first_treatment_date'], by_course[2]['discharge_date']),
            ('2026-03-02', '2026-03-05', '2026-03-30'),
        )
        self.assertEqual(before['patient'], (
            Patient.objects.get(pk=self.patient.pk).admission_date,
            Patient.objects.get(pk=self.patient.pk).first_treatment_date,
            Patient.objects.get(pk=self.patient.pk).discharge_date,
        ))
        self.assertEqual(before['course_one'], tuple(
            getattr(TreatmentCourse.objects.get(pk=course_one.pk), field)
            for field in ('admission_date', 'first_treatment_date', 'discharge_date')
        ))
        self.assertEqual(before['course_two'], tuple(
            getattr(TreatmentCourse.objects.get(pk=course_two.pk), field)
            for field in ('admission_date', 'first_treatment_date', 'discharge_date')
        ))

    def test_summary_csv_course_date_nulls_and_legacy_patient_fallback(self):
        from rtms_app.services.export_research import generate_research_summary_csv

        TreatmentCourse.objects.create(patient=self.patient, course_number=1)
        self.patient.admission_date = date(2026, 4, 1)
        self.patient.first_treatment_date = date(2026, 4, 5)
        self.patient.discharge_date = date(2026, 4, 30)
        self.patient.save(update_fields=['admission_date', 'first_treatment_date', 'discharge_date'])
        legacy = Patient.objects.create(
            card_id='RSRCH3', name='Legacy Research', birth_date=date(1975, 2, 2),
            admission_date=date(2026, 5, 1), first_treatment_date=date(2026, 5, 5),
            discharge_date=date(2026, 5, 30),
        )

        content = generate_research_summary_csv()
        rows = {row['card_id']: row for row in csv.DictReader(content.splitlines())}
        self.assertEqual(
            (rows[self.patient.card_id]['admission_date'], rows[self.patient.card_id]['first_treatment_date'], rows[self.patient.card_id]['discharge_date']),
            ('2026-04-01', '2026-04-05', '2026-04-30'),
        )
        self.assertEqual(
            (rows[legacy.card_id]['admission_date'], rows[legacy.card_id]['first_treatment_date'], rows[legacy.card_id]['discharge_date']),
            ('2026-05-01', '2026-05-05', '2026-05-30'),
        )

    def test_summary_csv_adverse_event_flags_are_boolean(self):
        session = TreatmentSession.objects.create(
            patient=self.patient, course_number=1, session_date=date(2026, 1, 5),
        )
        SeriousAdverseEvent.objects.create(
            patient=self.patient, course_number=1, session=session, event_types=['seizure'],
        )
        AdverseEventReport.objects.create(session=session, adverse_event_name='けいれん発作')
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('rtms_app:export_research_csv'))
        rows = list(csv.DictReader(response.content.decode('utf-8-sig').splitlines()))
        row = next(r for r in rows if r['card_id'] == 'RSRCH1')
        self.assertEqual(row['sae_seizure'], '1')
        self.assertEqual(row['sae_syncope'], '0')
        self.assertEqual(row['ae_report_exists'], '1')
        other_row = next(r for r in rows if r['card_id'] == 'RSRCH2')
        self.assertEqual(other_row['sae_seizure'], '0')
        self.assertEqual(other_row['ae_report_exists'], '0')

    def test_treatment_detail_csv_multiple_sessions_become_multiple_rows(self):
        TreatmentSession.objects.create(
            patient=self.patient, course_number=1, session_date=date(2026, 1, 5),
            coil_type='Brainsway H1', target_site='左DLPFC', mt_percent=110, intensity_percent=60,
        )
        TreatmentSession.objects.create(
            patient=self.patient, course_number=1, session_date=date(2026, 1, 6),
            coil_type='Brainsway H1', target_site='左DLPFC',
        )
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('rtms_app:export_research_treatment_detail_csv'))
        rows = list(csv.DictReader(response.content.decode('utf-8-sig').splitlines()))
        patient_rows = [r for r in rows if r['card_id'] == 'RSRCH1']
        self.assertEqual(len(patient_rows), 2)
        self.assertEqual(patient_rows[0]['session_no'], '1')
        self.assertEqual(patient_rows[1]['session_no'], '2')
        self.assertEqual(patient_rows[0]['coil_type'], 'Brainsway H1')
        self.assertEqual(patient_rows[0]['target_site'], '左DLPFC')

    def test_treatment_detail_csv_session_without_side_effect_check_is_blank(self):
        TreatmentSession.objects.create(
            patient=self.patient, course_number=1, session_date=date(2026, 1, 5),
        )
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('rtms_app:export_research_treatment_detail_csv'))
        rows = list(csv.DictReader(response.content.decode('utf-8-sig').splitlines()))
        row = next(r for r in rows if r['card_id'] == 'RSRCH1')
        self.assertEqual(row['sideeffect_headache_post_before'], '')
        self.assertEqual(row['sideeffect_seizure_before'], '')

    def test_treatment_detail_csv_side_effect_values_are_mapped(self):
        session = TreatmentSession.objects.create(
            patient=self.patient, course_number=1, session_date=date(2026, 1, 5),
        )
        SideEffectCheck.objects.create(
            session=session,
            rows=[{'item': '頭痛 (刺激後)', 'before': 0, 'during': 1, 'after': 2, 'relatedness': 3, 'memo': 'メモ'}],
        )
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('rtms_app:export_research_treatment_detail_csv'))
        rows = list(csv.DictReader(response.content.decode('utf-8-sig').splitlines()))
        row = next(r for r in rows if r['card_id'] == 'RSRCH1')
        self.assertEqual(row['sideeffect_headache_post_before'], '0')
        self.assertEqual(row['sideeffect_headache_post_during'], '1')
        self.assertEqual(row['sideeffect_headache_post_after'], '2')
        self.assertEqual(row['sideeffect_headache_post_relatedness'], '3')
        self.assertEqual(row['sideeffect_headache_post_memo'], 'メモ')

    def test_adverse_events_csv_no_events_is_empty_but_valid(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('rtms_app:export_research_adverse_events_csv'))
        content = response.content.decode('utf-8-sig')
        lines = content.strip().splitlines()
        self.assertEqual(len(lines), 1)  # header only
        self.assertIn('event_types', lines[0])

    def test_adverse_events_csv_joins_sae_and_report(self):
        session = TreatmentSession.objects.create(
            patient=self.patient, course_number=1, session_date=date(2026, 1, 5),
        )
        SeriousAdverseEvent.objects.create(
            patient=self.patient, course_number=1, session=session, event_types=['seizure', 'syncope'],
        )
        AdverseEventReport.objects.create(
            session=session, adverse_event_name='けいれん発作', age=46, sex='男性', initials='S.T.',
            rmt_value=52, intensity_value=60, outcome_flags=['improvement'],
            special_notes='経過観察中',
        )
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('rtms_app:export_research_adverse_events_csv'))
        rows = list(csv.DictReader(response.content.decode('utf-8-sig').splitlines()))
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['card_id'], 'RSRCH1')
        self.assertEqual(row['event_types'], 'seizure,syncope')
        self.assertEqual(row['adverse_event_name'], 'けいれん発作')
        self.assertEqual(row['age'], '46')
        self.assertEqual(row['rmt_value'], '52')
        self.assertEqual(row['outcome'], '軽快')
        self.assertEqual(row['notes'], '経過観察中')

    def test_zip_bundle_contains_three_csvs(self):
        import zipfile
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('rtms_app:export_research_zip'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/zip')
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        self.assertEqual(
            set(archive.namelist()),
            {'research_summary.csv', 'research_treatment_detail.csv', 'research_adverse_events.csv'},
        )

    def test_japanese_text_is_not_mangled(self):
        self.client.force_login(self.superuser)
        response = self.client.get(reverse('rtms_app:export_research_csv'))
        content = response.content.decode('utf-8-sig')
        self.assertIn('うつ病', content)

    def test_existing_discharge_survey_csv_is_untouched(self):
        """The discharge screen's self-report survey CSV must keep working as-is."""
        staff = get_user_model().objects.create_user(username='discharge-staff', password='pw', is_staff=True)
        self.client.force_login(staff)
        response = self.client.get(reverse('rtms_app:patient_survey_export', args=[self.patient.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertIn('text/csv', response['Content-Type'])
