from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
import re

from rtms_app.models import Patient
from rtms_app.services.patient_accounts import ensure_patient_user, PATIENT_GROUP_NAME


class Command(BaseCommand):
    help = "Create patient portal users for existing patients (card_id = username/password)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--reset-password",
            action="store_true",
            help="Reset password to the patient's card_id even if a user already exists.",
        )
        parser.add_argument(
            "--patient-id",
            type=int,
            action="append",
            dest="patient_ids",
            help="Target specific patient IDs (can be repeated).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Show what would be done without making changes.",
        )
        parser.add_argument(
            "--disable-pk-users",
            action="store_true",
            help="Disable old PK-based users (00001-99999 format not matching any card_id).",
        )

    def handle(self, *args, **options):
        dry_run = options.get("dry_run", False)
        disable_pk_users = options.get("disable_pk_users", False)
        
        # First, disable old PK-based users if requested
        if disable_pk_users:
            self._disable_old_pk_users(dry_run)
        
        qs = Patient.objects.all()
        if options.get("patient_ids"):
            qs = qs.filter(id__in=options["patient_ids"])

        created = 0
        touched = 0
        skipped = 0
        reset_pw = bool(options.get("reset_password"))

        for patient in qs.iterator():
            # Skip patients without valid card_id
            if not patient.card_id or not re.match(r'^\d{5}$', patient.card_id):
                self.stdout.write(self.style.WARNING(
                    f"Patient {patient.id} ({patient.name}): invalid card_id '{patient.card_id}' - skipped"
                ))
                skipped += 1
                continue
            
            if dry_run:
                status = "would create" if not patient.user_id else "would update"
                self.stdout.write(f"Patient {patient.id} ({patient.card_id}): {status} user '{patient.card_id}'")
                touched += 1
                continue
            
            try:
                user, was_created = ensure_patient_user(patient, reset_password=reset_pw)
                if was_created:
                    created += 1
                else:
                    touched += 1
                self.stdout.write(
                    f"Patient {patient.id} ({patient.card_id}): linked to user {user.username} "
                    f"({'created' if was_created else 'ok'})"
                )
            except Exception as e:
                self.stdout.write(self.style.ERROR(
                    f"Patient {patient.id} ({patient.card_id}): failed - {e}"
                ))
                skipped += 1

        self.stdout.write(self.style.SUCCESS(
            f"Finished. created={created}, touched={touched}, skipped={skipped}"
        ))
    
    def _disable_old_pk_users(self, dry_run: bool):
        """Disable old PK-based patient users that don't match any card_id."""
        from django.contrib.auth.models import Group
        
        try:
            patient_group = Group.objects.get(name=PATIENT_GROUP_NAME)
        except Group.DoesNotExist:
            self.stdout.write(self.style.WARNING(f"Group '{PATIENT_GROUP_NAME}' not found, skipping PK user cleanup"))
            return
        
        # Get all valid card_ids
        valid_card_ids = set(
            Patient.objects.filter(card_id__regex=r'^\d{5}$')
            .values_list('card_id', flat=True)
        )
        
        # Find PK-format users in patient group that don't match any card_id
        pk_users = User.objects.filter(
            groups=patient_group,
            username__regex=r'^\d{5}$',
            is_active=True
        ).exclude(username__in=valid_card_ids)
        
        count = pk_users.count()
        if count == 0:
            self.stdout.write("No old PK-based users to disable")
            return
        
        if dry_run:
            self.stdout.write(self.style.WARNING(
                f"Would disable {count} old PK-based users:"
            ))
            for user in pk_users[:10]:  # Show first 10
                self.stdout.write(f"  - {user.username}")
            if count > 10:
                self.stdout.write(f"  ... and {count - 10} more")
        else:
            pk_users.update(is_active=False)
            self.stdout.write(self.style.SUCCESS(
                f"Disabled {count} old PK-based users"
            ))

