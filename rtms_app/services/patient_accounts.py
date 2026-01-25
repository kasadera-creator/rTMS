from __future__ import annotations
from typing import Tuple

from django.contrib.auth.models import Group, User
from django.db import transaction

from rtms_app.models import Patient

PATIENT_GROUP_NAME = "patient"


def ensure_patient_group() -> Group:
    group, _ = Group.objects.get_or_create(name=PATIENT_GROUP_NAME)
    return group


def ensure_patient_user(patient: Patient, reset_password: bool = False) -> Tuple[User, bool]:
    """Create or attach a patient portal user for the given Patient.

    - username/password = patient.card_id (5 digits)
    - user added to PATIENT_GROUP_NAME
    - Patient.user is linked
    """
    import re
    if not patient.pk:
        raise ValueError("Patient must be saved before creating a user")
    
    if not patient.card_id or not re.match(r'^\d{5}$', patient.card_id):
        raise ValueError(f"Patient.card_id must be exactly 5 digits, got: {patient.card_id}")

    group = ensure_patient_group()
    username = patient.card_id

    user = patient.user
    created = False
    if user is None:
        user, created = User.objects.get_or_create(
            username=username,
            defaults={
                "is_active": True,
                "is_staff": False,
                "is_superuser": False,
            },
        )
    else:
        if user.username != username:
            user.username = username

    if reset_password or created:
        user.set_password(username)

    # Keep minimal identifying info; avoid overriding staff flags
    if not user.first_name and patient.name:
        user.first_name = patient.name
    user.save()
    user.groups.add(group)

    if patient.user_id != user.id:
        # Avoid recursive signals by updating directly
        Patient.objects.filter(pk=patient.pk).update(user=user)
        patient.user = user

    return user, created


def reset_patient_password(patient: Patient) -> User:
    user, _ = ensure_patient_user(patient, reset_password=True)
    return user


__all__ = ["ensure_patient_user", "ensure_patient_group", "reset_patient_password", "PATIENT_GROUP_NAME"]
