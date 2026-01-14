from django.test import Client
from django.contrib.auth import get_user_model
from rtms_app.models import Patient, TreatmentSession, TreatmentSkip
from django.urls import reverse
import datetime

User = get_user_model()
# create unique temporary user
User.objects.filter(username='__tmp_debug__').delete()
user = User.objects.create_user(username='__tmp_debug__', password='pw')
client = Client()
# Bypass ALLOWED_HOSTS check by using the SERVER_NAME from settings
from django.conf import settings
client.defaults['HTTP_HOST'] = 'localhost' if 'localhost' in settings.ALLOWED_HOSTS else (settings.ALLOWED_HOSTS[0] if settings.ALLOWED_HOSTS else 'localhost')
client.login(username='__tmp_debug__', password='pw')

patient = Patient.objects.create(card_id='DBG1', name='Dbg Test', birth_date=datetime.date(1990,1,1))
from datetime import date
s1 = TreatmentSession.objects.create(patient=patient, session_date=date(2026,1,5))
s2 = TreatmentSession.objects.create(patient=patient, session_date=date(2026,1,6))
s3 = TreatmentSession.objects.create(patient=patient, session_date=date(2026,1,7))
patient.discharge_date = date(2026,1,31); patient.save()

print('URL reversed:', reverse('rtms_app:treatment_add', args=[patient.id]))
url = reverse('rtms_app:treatment_add', args=[patient.id])
post = {
    'treatment_date': date(2026,1,6).isoformat(),
    'treatment_time': '09:00',
    'mt_percent': '120',
    'frequency_hz': '18.0',
    'train_seconds': '2.0',
    'intertrain_seconds': '20.0',
    'train_count': '55',
    'total_pulses': '1980',
    'action': 'skip',
    'skip_reason': 'manual debug',
}
print("POST data:", post)
print(f"Making POST to: {url}")
resp = client.post(url, post, follow=False)
print('RESP STATUS:', resp.status_code)
print('Location:', resp.get('Location'))
print('Response object:', resp)
print('Response URL:', getattr(resp, 'url', 'N/A'))
if resp.status_code == 302:
    print('Redirected to:', resp.get('Location'))
    # Check the redirect destination
    if resp.get('Location') and '/dashboard/' not in resp.get('Location'):
        print('WARNING: Not redirected to dashboard, redirected to:', resp.get('Location'))
    # Try to follow the redirect
    print('Attempting to follow redirect...')
    resp2 = client.get(resp.get('Location'), follow=False)
    print('After redirect: status=', resp2.status_code, 'url=', getattr(resp2, 'url', 'N/A'))
print('TreatmentSkip count:', TreatmentSkip.objects.filter(treatment__patient=patient).count())
try:
    print('RTMS SKIP DEBUG LOG:')
    print(open('/tmp/rtms_skip_debug.log').read())
except Exception as e:
    print('no /tmp log', e)
try:
    print('TREATMENTSKIP SAVE LOG:')
    print(open('/tmp/rtms_treatmentskip.log').read())
except Exception as e:
    print('no /tmp treatmentskip log', e)
