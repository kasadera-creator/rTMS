#!/usr/bin/env python
"""
Test patient user creation with card_id.
"""
import os
import sys
import django

os.chdir('/Users/kuniyuki/rTMS')
sys.path.insert(0, '/Users/kuniyuki/rTMS')
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

from django.contrib.auth.models import User
from rtms_app.models import Patient
from rtms_app.services.patient_accounts import ensure_patient_user
import re

print("=== Patient User Creation Test ===\n")

# Test 1: Check existing patient
patients = Patient.objects.all()
print(f"Total patients: {patients.count()}")

for patient in patients[:5]:
    print(f"\nPatient ID: {patient.id}")
    print(f"  card_id: {patient.card_id}")
    print(f"  name: {patient.name}")
    print(f"  user: {patient.user}")
    
    if patient.user:
        print(f"  username: {patient.user.username}")
        print(f"  is_active: {patient.user.is_active}")
        print(f"  groups: {', '.join(g.name for g in patient.user.groups.all())}")
        
        # Check if username matches card_id
        if patient.user.username == patient.card_id:
            print("  ✓ Username matches card_id")
        else:
            print(f"  ✗ Username mismatch: expected '{patient.card_id}', got '{patient.user.username}'")
    else:
        print("  ⚠ No user linked")

# Test 2: Verify card_id format validation
print("\n\n=== Card ID Validation Test ===")
test_cases = [
    ("12345", True, "Valid 5-digit"),
    ("00123", True, "Valid with leading zeros"),
    ("1234", False, "Too short"),
    ("123456", False, "Too long"),
    ("abcde", False, "Non-numeric"),
    ("12 345", False, "Contains space"),
]

for card_id, should_pass, desc in test_cases:
    valid = bool(re.match(r'^\d{5}$', card_id))
    status = "✓" if (valid == should_pass) else "✗"
    print(f"{status} {desc}: '{card_id}' -> {'Valid' if valid else 'Invalid'}")

print("\n=== Test Complete ===")
