from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model

from rtms_app import assessment_rules
from rtms_app.models import Patient
import datetime
from datetime import date
from rtms_app import services
from rtms_app.services import schedule as schedule_service


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

    def test_skip_undo_restores_original_dates(self):
        from datetime import date
        from rtms_app.models import TreatmentSkip
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
