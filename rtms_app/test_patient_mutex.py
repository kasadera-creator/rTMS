from datetime import date
from unittest import skipUnless
from unittest.mock import Mock, patch

from django.db import NotSupportedError, connection, transaction
from django.db.models import QuerySet
from django.db.models.signals import post_save
from django.test import TransactionTestCase

from rtms_app.models import Patient
from rtms_app.services.patient_mutex import acquire_patient_mutex


class PatientMutexTests(TransactionTestCase):
    reset_sequences = True

    def create_patient(self):
        return Patient.objects.create(
            card_id="12345",
            name="Mutex Patient",
            birth_date=date(1980, 1, 1),
            course_number=2,
            mapping_date=date(2026, 9, 1),
            first_treatment_date=date(2026, 9, 2),
        )

    def test_postgresql_backend_uses_select_for_update(self):
        patient = self.create_patient()
        manager = Patient.objects

        with patch.object(connection, "vendor", "postgresql"):
            with patch.object(
                manager,
                "select_for_update",
                wraps=manager.select_for_update,
            ) as select_for_update:
                with transaction.atomic():
                    locked_patient = acquire_patient_mutex(patient.pk)

        select_for_update.assert_called_once_with()
        self.assertEqual(locked_patient.pk, patient.pk)

    @skipUnless(connection.vendor == "sqlite", "requires a real SQLite test database")
    def test_sqlite_backend_reserves_writer_and_preserves_patient_data(self):
        patient = self.create_patient()
        concrete_fields = [field.attname for field in Patient._meta.concrete_fields]
        before = Patient.objects.filter(pk=patient.pk).values(*concrete_fields).get()
        post_save_receiver = Mock()
        post_save.connect(
            post_save_receiver,
            sender=Patient,
            weak=False,
            dispatch_uid="patient-mutex-no-post-save",
        )
        try:
            with transaction.atomic():
                locked_patient = acquire_patient_mutex(patient.pk)
        finally:
            post_save.disconnect(
                sender=Patient,
                dispatch_uid="patient-mutex-no-post-save",
            )

        after = Patient.objects.filter(pk=patient.pk).values(*concrete_fields).get()
        self.assertEqual(locked_patient.pk, patient.pk)
        self.assertEqual(after, before)
        post_save_receiver.assert_not_called()
        if "updated_at" in concrete_fields:
            self.assertEqual(after["updated_at"], before["updated_at"])

    @skipUnless(connection.vendor == "sqlite", "requires a real SQLite test database")
    def test_sqlite_backend_rejects_missing_patient(self):
        with transaction.atomic():
            with self.assertRaises(Patient.DoesNotExist):
                acquire_patient_mutex(999999)

    def test_unsupported_backend_fails_closed(self):
        with patch.object(connection, "vendor", "mysql"):
            with transaction.atomic():
                with self.assertRaises(NotSupportedError):
                    acquire_patient_mutex(1)

    def test_call_outside_atomic_transaction_is_rejected(self):
        self.assertFalse(connection.in_atomic_block)
        with self.assertRaises(RuntimeError):
            acquire_patient_mutex(1)

    def test_patient_does_not_have_updated_at_auto_field(self):
        self.assertNotIn(
            "updated_at",
            {field.name for field in Patient._meta.concrete_fields},
        )
