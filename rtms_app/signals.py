from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.db import transaction
from django.contrib.auth import get_user_model
from .models import AuditLog, TreatmentSession, Assessment, ConsentDocument, Patient
from .services.patient_accounts import ensure_patient_user
from .utils.request_context import get_current_request, get_client_ip, get_user_agent
import re

TARGET_MODELS = [TreatmentSession, Assessment]

def create_audit_log(instance, action, summary='', meta=None):
    request = get_current_request()
    if not request or not request.user.is_authenticated:
        return
    
    patient = None
    if hasattr(instance, 'patient'):
        patient = instance.patient
    elif isinstance(instance, Patient):
        patient = instance
    
    if not patient:
        return
    
    ip = get_client_ip(request)
    user_agent = get_user_agent(request)
    
    meta = meta or {}
    meta['course_number'] = getattr(patient, 'course_number', 1)
    
    def _create_log():
        AuditLog.objects.create(
            user=request.user,
            patient=patient,
            target_model=instance.__class__.__name__,
            target_pk=str(instance.pk),
            action=action,
            summary=summary,
            meta=meta,
            ip=ip,
            user_agent=user_agent,
        )
    
    transaction.on_commit(_create_log)

@receiver(post_save)
def audit_log_save(sender, instance, created, **kwargs):
    if sender not in TARGET_MODELS:
        return
    
    action = 'CREATE' if created else 'UPDATE'
    summary = f"{instance.__class__.__name__} {action.lower()}d"
    meta = {}
    if not created:
        # 更新時は変更点を記録（簡易版）
        update_fields = kwargs.get("update_fields")
        meta = {'updated_fields': list(update_fields) if update_fields else []}
    
    create_audit_log(instance, action, summary, meta)

@receiver(post_delete)
def audit_log_delete(sender, instance, **kwargs):
    if sender not in TARGET_MODELS:
        return
    
    summary = f"{instance.__class__.__name__} deleted"
    create_audit_log(instance, 'DELETE', summary)


# --- AuditLog for User model actions ---
User = get_user_model()


@receiver(post_save, sender=User)
def audit_log_user_save(sender, instance, created, **kwargs):
    """Log user create/update actions to AuditLog."""
    request = get_current_request()
    if not request or not request.user.is_authenticated:
        return

    action = 'CREATE' if created else 'UPDATE'
    ip = get_client_ip(request)
    user_agent = get_user_agent(request)

    def _create_user_log():
        AuditLog.objects.create(
            user=request.user,
            patient=None,
            target_model='User',
            target_pk=str(instance.pk),
            action=action,
            summary=f"User {'created' if created else 'updated'}: {getattr(instance, 'username', str(instance.pk))}",
            meta={},
            ip=ip,
            user_agent=user_agent,
        )

    transaction.on_commit(_create_user_log)


@receiver(post_save, sender=Patient)
def auto_create_patient_user(sender, instance, created, **kwargs):
    """Automatically create patient portal user when Patient is created."""
    if not instance.pk or not instance.card_id:
        return
    
    # Only create user if card_id is valid 5-digit format
    if not re.match(r'^\d{5}$', instance.card_id):
        return
    
    # Skip if user already exists
    if instance.user_id:
        return
    
    try:
        ensure_patient_user(instance)
    except Exception as e:
        # Log error but don't break patient creation
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Failed to auto-create user for patient {instance.pk}: {e}")


@receiver(post_delete, sender=User)
def audit_log_user_delete(sender, instance, **kwargs):
    request = get_current_request()
    if not request or not request.user.is_authenticated:
        return

    ip = get_client_ip(request)
    user_agent = get_user_agent(request)

    def _create_user_delete_log():
        AuditLog.objects.create(
            user=request.user,
            patient=None,
            target_model='User',
            target_pk=str(getattr(instance, 'pk', '')),
            action='DELETE',
            summary=f"User deleted: {getattr(instance, 'username', str(getattr(instance, 'pk', '')))}",
            meta={},
            ip=ip,
            user_agent=user_agent,
        )

    transaction.on_commit(_create_user_delete_log)


@receiver(post_save, sender=Patient)
def create_patient_user_on_save(sender, instance: Patient, created, **kwargs):
    """Automatically provision a patient portal user when a Patient is saved."""
    try:
        if created or not instance.user_id:
            ensure_patient_user(instance)
    except Exception:
        # Avoid breaking patient save due to user provisioning errors
        pass