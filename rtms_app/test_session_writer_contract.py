from datetime import date
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import connection
from django.db.models.signals import post_save
from django.test import Client, TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from rtms_app.models import (
    MappingSchedule,
    MappingSession,
    Patient,
    TreatmentCourse,
    TreatmentSession,
)
from rtms_app.services import session_writes
from rtms_app.services.session_writes import (
    PatientCourseWriteError,
    patient_course_writer,
)
from rtms_app.signals import audit_log_user_save


class PatientCourseWriterTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        self.patient = Patient.objects.create(
            card_id="WRITER001",
            name="Writer Boundary Patient",
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

    def test_mutex_is_first_database_operation_and_course_is_resolved_after_it(self):
        with CaptureQueriesContext(connection) as captured:
            with patient_course_writer(self.patient.pk, 2) as scope:
                self.assertEqual(scope.patient.pk, self.patient.pk)
                self.assertEqual(scope.treatment_course.pk, self.course_two.pk)

        queries = [
            query["sql"].strip().lower()
            for query in captured.captured_queries
            if not query["sql"].lstrip().lower().startswith(
                ("begin", "savepoint", "release savepoint", "commit", "rollback")
            )
        ]
        self.assertTrue(queries)
        if connection.vendor == "sqlite":
            self.assertIn('update "rtms_app_patient"', queries[0])
        elif connection.vendor == "postgresql":
            self.assertIn('from "rtms_app_patient"', queries[0])
            self.assertIn("for update", queries[0])
        else:
            self.fail(f"Unsupported test backend: {connection.vendor}")
        self.assertIn('from "rtms_app_treatmentcourse"', queries[2])

    def test_nested_course_writer_reuses_patient_mutex_and_keeps_course_scope(self):
        with patch.object(
            session_writes,
            "acquire_patient_mutex",
            wraps=session_writes.acquire_patient_mutex,
        ) as acquire_mutex:
            with patient_course_writer(self.patient.pk, 1) as course_one_scope:
                with patient_course_writer(self.patient.pk, 2) as course_two_scope:
                    self.assertEqual(course_one_scope.treatment_course.pk, self.course_one.pk)
                    self.assertEqual(course_two_scope.treatment_course.pk, self.course_two.pk)

        acquire_mutex.assert_called_once_with(self.patient.pk)

    def test_default_course_is_resolved_from_patient_after_mutex(self):
        stale_patient = self.patient
        Patient.objects.filter(pk=self.patient.pk).update(course_number=2)

        with patient_course_writer(stale_patient.pk) as scope:
            self.assertEqual(scope.course_number, 2)
            self.assertEqual(scope.treatment_course.pk, self.course_two.pk)

    def test_explicit_missing_or_mismatched_course_fails_closed(self):
        with self.assertRaises(PatientCourseWriteError):
            with patient_course_writer(self.patient.pk, 3, require_course=True):
                pass

        with self.assertRaises(PatientCourseWriteError):
            with patient_course_writer(
                self.patient.pk,
                1,
                expected_course_id=self.course_two.pk,
            ):
                pass

    def test_session_filter_is_course_specific_and_legacy_is_explicit(self):
        with patient_course_writer(self.patient.pk, 1) as strict_scope:
            self.assertEqual(strict_scope.session_filter, {"treatment_course": self.course_one})

        legacy_patient = Patient.objects.create(
            card_id="WRITER002",
            name="Legacy Writer Patient",
            birth_date=date(1981, 1, 1),
            course_number=1,
        )
        with patient_course_writer(legacy_patient.pk, 1) as legacy_scope:
            self.assertIsNone(legacy_scope.treatment_course)
            self.assertEqual(
                legacy_scope.session_filter,
                {
                    "patient": legacy_patient,
                    "course_number": 1,
                    "treatment_course__isnull": True,
                },
            )

    def test_mixed_legacy_and_course_rows_fail_closed(self):
        TreatmentSession.objects.create(
            patient=self.patient,
            course_number=1,
            session_date=date(2026, 9, 7),
            treatment_course=None,
        )

        with self.assertRaises(PatientCourseWriteError):
            with patient_course_writer(self.patient.pk, 1):
                pass

    def test_mapping_session_fk_course_number_mismatch_fails_closed(self):
        MappingSession.objects.create(
            patient=self.patient,
            treatment_course=self.course_two,
            course_number=1,
            date=date(2026, 9, 7),
            week_number=1,
            resting_mt=50,
        )

        with self.assertRaises(PatientCourseWriteError):
            with patient_course_writer(self.patient.pk, 1):
                pass


class SessionWriterViewTests(TransactionTestCase):
    reset_sequences = True

    def setUp(self):
        user_model = get_user_model()
        post_save.disconnect(audit_log_user_save, sender=user_model)
        self.addCleanup(
            post_save.connect,
            audit_log_user_save,
            sender=user_model,
            weak=False,
        )
        self.user = user_model.objects.create_user(
            username="session-writer-user",
            password="test-password",
        )
        self.client = Client()
        self.client.force_login(self.user)
        self.patient = Patient.objects.create(
            card_id="WRITERVIEW1",
            name="Session Writer View Patient",
            birth_date=date(1980, 1, 1),
            course_number=1,
            first_treatment_date=date(2026, 9, 7),
        )
        self.course_one = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=1,
            first_treatment_date=date(2026, 9, 7),
        )
        self.course_two = TreatmentCourse.objects.create(
            patient=self.patient,
            course_number=2,
            first_treatment_date=date(2026, 10, 5),
        )

    def test_mapping_add_updates_only_matching_course_and_creates_new_day(self):
        course_one_mapping = MappingSession.objects.create(
            patient=self.patient,
            treatment_course=self.course_one,
            course_number=1,
            date=date(2026, 9, 7),
            week_number=1,
            resting_mt=50,
        )
        course_two_mapping = MappingSession.objects.create(
            patient=self.patient,
            treatment_course=self.course_two,
            course_number=2,
            date=date(2026, 9, 7),
            week_number=1,
            resting_mt=60,
        )
        url = reverse("rtms_app:mapping_add", args=[self.patient.pk])

        with patch.object(
            session_writes,
            "acquire_patient_mutex",
            wraps=session_writes.acquire_patient_mutex,
        ) as acquire_mutex:
            response = self.client.post(url, {
                "course_number": "2",
                "date": "2026-09-07",
                "week_number": "1",
                "resting_mt": "70",
            })

        self.assertEqual(response.status_code, 302)
        acquire_mutex.assert_called_once_with(self.patient.pk)
        course_one_mapping.refresh_from_db()
        course_two_mapping.refresh_from_db()
        self.assertEqual(course_one_mapping.resting_mt, 50)
        self.assertEqual(course_two_mapping.resting_mt, 70)

        response = self.client.post(url, {
            "course_number": "2",
            "date": "2026-09-08",
            "week_number": "1",
            "resting_mt": "80",
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            MappingSession.objects.filter(treatment_course=self.course_two).count(),
            2,
        )
        self.assertEqual(
            MappingSession.objects.filter(treatment_course=self.course_one).count(),
            1,
        )
