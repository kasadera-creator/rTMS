from django.db import NotSupportedError, connection
from django.db.models import F

from rtms_app.models import Patient


def acquire_patient_mutex(patient_id):
    """Lock an existing Patient row inside the caller's atomic transaction.

    Call this before any other database operation in the transaction. PostgreSQL
    uses a row lock; SQLite starts a write transaction with a no-op UPDATE.
    """
    if not connection.in_atomic_block:
        raise RuntimeError("Patient mutex requires an active atomic transaction")

    if connection.vendor == "postgresql":
        return Patient.objects.select_for_update().get(pk=patient_id)

    if connection.vendor == "sqlite":
        updated_rows = Patient.objects.filter(pk=patient_id).update(
            course_number=F("course_number"),
        )
        if updated_rows != 1:
            raise Patient.DoesNotExist(f"Patient matching query does not exist: {patient_id}")
        return Patient.objects.get(pk=patient_id)

    raise NotSupportedError(
        f"Patient mutex is not supported by database backend: {connection.vendor}"
    )