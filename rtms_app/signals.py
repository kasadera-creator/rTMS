from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.db import transaction
from .models import AuditLog, TreatmentSession, Assessment, ConsentDocument, Patient
from .utils.request_context import get_current_request, get_client_ip, get_user_agent

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