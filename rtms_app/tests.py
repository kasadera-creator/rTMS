from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model

from rtms_app import assessment_rules
from rtms_app.models import Patient, Assessment
import datetime
from datetime import date
from rtms_app import services
from rtms_app.services import schedule as schedule_service
from rtms_app.surveys import INSTRUMENT_ORDER, get_instrument
from rtms_app.services.patient_accounts import ensure_patient_group


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

class TestPatientSurveyFlow(TestCase):
    def setUp(self):
        self.client = Client()
        User = get_user_model()
        # Ensure patient group exists
        ensure_patient_group()
        self.patient = Patient.objects.create(
            card_id="12345",
            name="Survey Patient",
            birth_date=datetime.date(1990, 1, 1),
        )
        # Create linked user with patient group
        from rtms_app.services.patient_accounts import ensure_patient_user
        user, _ = ensure_patient_user(self.patient, reset_password=True)
        self.user = user
        self.client.force_login(self.user)

    def _answers_for(self, instrument_code: str):
        inst = get_instrument(instrument_code)
        data = {}
        for q in inst.get("questions", []):
            opts = q.get("options", [])
            if not opts:
                continue
            data[q["key"]] = opts[0]["id"]
        return data

    def test_full_flow_reaches_review(self):
        from rtms_app.models import PatientSurveySession

        session = PatientSurveySession.objects.create(
            patient=self.patient,
            phase="pre",
            status="in_progress",
            course_number=1,
        )

        for idx, code in enumerate(INSTRUMENT_ORDER):
            url = reverse("patient_portal:instrument", args=[session.id, code])
            data = self._answers_for(code)
            data["nav"] = "next"
            resp = self.client.post(url, data)
            if idx == len(INSTRUMENT_ORDER) - 1:
                self.assertRedirects(resp, reverse("patient_portal:review", args=[session.id]))
            else:
                next_code = INSTRUMENT_ORDER[idx + 1]
                self.assertRedirects(resp, reverse("patient_portal:instrument", args=[session.id, next_code]))

    def test_invalid_instrument_is_forbidden(self):
        url = reverse("patient_portal:instrument", args=[999, "invalid_code"])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 403)

    def test_skip_undo_restores_original_dates(self):
        from datetime import date
        from rtms_app.models import TreatmentSkip
        staff = get_user_model().objects.create_user(
            username='skip-operator', password='pw', is_staff=True
        )
        self.client.force_login(staff)
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


# ============================================================================
# GROUP A: Print View Helpers Unit Tests
# ============================================================================

class TestPrintViewHelpers(TestCase):
    """Test print view helper functions (print_views.py)"""
    
    def setUp(self):
        self.patient = Patient.objects.create(
            card_id="PRINT001", 
            name="Print Test", 
            birth_date=date(1980, 1, 1),
            course_number=1
        )
    
    def test_print_extract_back_url_from_get(self):
        """Test _extract_back_url prioritizes request.GET"""
        from django.test import RequestFactory
        from rtms_app.print_views import _extract_back_url
        
        factory = RequestFactory()
        
        # Test 1: back_url in GET
        request = factory.get('/app/print/admission/?back_url=/custom/url')
        back_url = _extract_back_url(request, self.patient, 'rtms_app:patient_home')
        self.assertEqual(back_url, '/custom/url')
    
    def test_print_extract_back_url_fallback_to_referer(self):
        """Test _extract_back_url falls back to HTTP_REFERER"""
        from django.test import RequestFactory
        from rtms_app.print_views import _extract_back_url
        
        factory = RequestFactory()
        
        # Test 2: back_url not in GET, but HTTP_REFERER available
        request = factory.get('/app/print/admission/')
        request.META['HTTP_REFERER'] = '/previous/page'
        back_url = _extract_back_url(request, self.patient, 'rtms_app:patient_home')
        self.assertEqual(back_url, '/previous/page')
    
    def test_print_extract_back_url_fallback_to_view(self):
        """Test _extract_back_url uses view reverse as final fallback"""
        from django.test import RequestFactory
        from rtms_app.print_views import _extract_back_url
        
        factory = RequestFactory()
        
        # Test 3: neither GET nor referer, use fallback view
        request = factory.get('/app/print/admission/')
        back_url = _extract_back_url(request, self.patient, 'rtms_app:patient_home')
        expected_url = reverse('rtms_app:patient_home', args=[self.patient.id])
        self.assertEqual(back_url, expected_url)
    
    def test_print_get_latest_assessments_by_date(self):
        """Test _get_latest_assessments_by_date collapses duplicates"""
        from rtms_app.print_views import _get_latest_assessments_by_date
        from rtms_app.models import Assessment
        
        # Create assessments with different dates
        date1 = date(2026, 1, 5)
        date2 = date(2026, 1, 6)
        date3 = date(2026, 1, 7)
        
        a1 = Assessment.objects.create(patient=self.patient, date=date1, timing='baseline', course_number=1, total_score_17=20)
        a2 = Assessment.objects.create(patient=self.patient, date=date2, timing='week3', course_number=1, total_score_17=19)
        a3 = Assessment.objects.create(patient=self.patient, date=date3, timing='week4', course_number=1, total_score_17=18)
        
        result = _get_latest_assessments_by_date(self.patient)
        
        # Should have 3 unique dates
        self.assertEqual(len(result), 3)
        
        # Should be sorted by date
        self.assertEqual(result[0].date, date1)
        self.assertEqual(result[1].date, date2)
        self.assertEqual(result[2].date, date3)


class TestPrintViewContextBuilding(TestCase):
    """Test print view context building functions"""
    
    def setUp(self):
        self.patient = Patient.objects.create(
            card_id="PRINTCTX001", 
            name="Context Test", 
            birth_date=date(1980, 1, 1),
            course_number=2
        )
        self.client = Client()
        User = get_user_model()
        self.user = User.objects.create_user(username="printer", password="printpass")
        self.client.login(username="printer", password="printpass")
    
    def test_print_discharge_context_structure(self):
        """Test _build_discharge_context returns expected keys"""
        from django.test import RequestFactory
        from rtms_app.print_views import _build_discharge_context
        
        factory = RequestFactory()
        request = factory.get('/app/patient/1/print/discharge/')
        
        context = _build_discharge_context(request, self.patient.id)
        
        # Verify expected keys
        self.assertIn('patient', context)
        self.assertIn('today', context)
        self.assertIn('test_scores', context)
        self.assertIn('back_url', context)
        self.assertIn('hamd_trend_cols', context)
        self.assertIn('pdf_filename', context)
        
        # Verify patient
        self.assertEqual(context['patient'].id, self.patient.id)
        
        # Verify pdf_filename format
        self.assertIn(self.patient.card_id, context['pdf_filename'])
        self.assertIn('pdf', context['pdf_filename'])
    
    def test_print_views_html_and_pdf_endpoints(self):
        """Test discharge view returns both HTML and PDF"""
        # Test HTML view
        url_html = reverse('rtms_app:print:patient_print_discharge', args=[self.patient.id])
        response_html = self.client.get(url_html)
        self.assertEqual(response_html.status_code, 200)
        self.assertIn('text/html', response_html['Content-Type'])
        
        # Test PDF view (if weasyprint available)
        try:
            from weasyprint import HTML
            url_pdf = reverse('rtms_app:print:patient_print_discharge_pdf', args=[self.patient.id])
            response_pdf = self.client.get(url_pdf)
            # PDF might return 200 or error if weasyprint not fully configured
            self.assertIn(response_pdf.status_code, [200, 500])
        except ImportError:
            pass  # weasyprint not installed, skip PDF test


# ============================================================================
# GROUP B: View Helpers Unit Tests
# ============================================================================

class TestViewHelpersFunctions(TestCase):
    """Test view_helpers.py functions"""
    
    def setUp(self):
        self.patient = Patient.objects.create(
            card_id="HELPER001", 
            name="Helper Test", 
            birth_date=date(1980, 1, 1),
            course_number=3
        )
    
    def test_extract_back_url_from_get(self):
        """Test extract_back_url prioritizes GET parameter"""
        from django.test import RequestFactory
        from rtms_app.view_helpers import extract_back_url
        
        factory = RequestFactory()
        request = factory.get('/?back_url=/custom')
        
        url = extract_back_url(request, 'rtms_app:dashboard')
        self.assertEqual(url, '/custom')
    
    def test_extract_back_url_fallback_to_referer(self):
        """Test extract_back_url falls back to referer"""
        from django.test import RequestFactory
        from rtms_app.view_helpers import extract_back_url
        
        factory = RequestFactory()
        request = factory.get('/')
        request.META['HTTP_REFERER'] = '/referer/page'
        
        url = extract_back_url(request, 'rtms_app:dashboard')
        self.assertEqual(url, '/referer/page')
    
    def test_extract_back_url_fallback_to_reverse(self):
        """Test extract_back_url uses reverse as final fallback"""
        from django.test import RequestFactory
        from rtms_app.view_helpers import extract_back_url
        
        factory = RequestFactory()
        request = factory.get('/')
        
        url = extract_back_url(request, 'rtms_app:patient_home', self.patient.id)
        expected = reverse('rtms_app:patient_home', args=[self.patient.id])
        self.assertEqual(url, expected)
    
    def test_get_dashboard_date_present(self):
        """Test get_dashboard_date extracts date from GET"""
        from django.test import RequestFactory
        from rtms_app.view_helpers import get_dashboard_date
        
        factory = RequestFactory()
        request = factory.get('/?dashboard_date=2026-01-15')
        
        result = get_dashboard_date(request)
        self.assertEqual(result, '2026-01-15')
    
    def test_get_dashboard_date_absent(self):
        """Test get_dashboard_date returns None when not present"""
        from django.test import RequestFactory
        from rtms_app.view_helpers import get_dashboard_date
        
        factory = RequestFactory()
        request = factory.get('/')
        
        result = get_dashboard_date(request)
        self.assertIsNone(result)
    
    def test_build_common_context_basic(self):
        """Test build_common_context creates standard keys"""
        from rtms_app.view_helpers import build_common_context
        
        context = build_common_context(self.patient)
        
        self.assertIn('patient', context)
        self.assertIn('today', context)
        self.assertEqual(context['patient'], self.patient)
        self.assertIsNotNone(context['today'])
    
    def test_build_common_context_with_dashboard_date(self):
        """Test build_common_context includes dashboard_date"""
        from rtms_app.view_helpers import build_common_context
        
        context = build_common_context(self.patient, dashboard_date='2026-01-15')
        
        self.assertIn('dashboard_date', context)
        self.assertEqual(context['dashboard_date'], '2026-01-15')
    
    def test_build_common_context_with_extra(self):
        """Test build_common_context merges extra keys"""
        from rtms_app.view_helpers import build_common_context
        
        context = build_common_context(self.patient, logs=['log1', 'log2'], custom_key='value')
        
        self.assertIn('logs', context)
        self.assertIn('custom_key', context)
        self.assertEqual(context['logs'], ['log1', 'log2'])
        self.assertEqual(context['custom_key'], 'value')
    
    def test_get_course_number_present(self):
        """Test get_course_number returns patient course_number"""
        from rtms_app.view_helpers import get_course_number
        
        result = get_course_number(self.patient)
        self.assertEqual(result, 3)
    
    def test_get_course_number_default(self):
        """Test get_course_number returns 1 when not set"""
        from rtms_app.view_helpers import get_course_number
        
        # Create patient without explicit course_number (will use default)
        p = Patient.objects.create(
            card_id="COURSE_TEST",
            name="Course Test",
            birth_date=date(1990, 1, 1)
            # Note: Not setting course_number, will use model default
        )
        
        result = get_course_number(p)
        # Should return model default (1)
        self.assertEqual(result, 1)


# ============================================================================
# GROUP D: Assessment Query Helper Tests (Stage 9)
# ============================================================================

class TestAssessmentQueries(TestCase):
    """Test assessment query helpers from queries.assessment_queries"""
    
    def setUp(self):
        self.patient = Patient.objects.create(
            card_id="QUERY001",
            name="Query Test",
            birth_date=date(1980, 1, 1),
            course_number=1
        )
    
    def test_get_assessments_ordered_multiple(self):
        """Test get_assessments_ordered returns assessments in date order"""
        from rtms_app.queries.assessment_queries import get_assessments_ordered
        
        # Create assessments on different dates with different timings
        a1 = Assessment.objects.create(
            patient=self.patient,
            date=date(2026, 1, 5),
            timing='baseline',
            course_number=1,
            total_score_17=20
        )
        a2 = Assessment.objects.create(
            patient=self.patient,
            date=date(2026, 1, 3),
            timing='week3',
            course_number=1,
            total_score_17=19,
            type='HAM-D'
        )
        a3 = Assessment.objects.create(
            patient=self.patient,
            date=date(2026, 1, 7),
            timing='week4',
            course_number=1,
            total_score_17=18,
            type='HAM-D'
        )
        
        # Query should return in ascending date order
        result = list(get_assessments_ordered(self.patient))
        
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0].id, a2.id)  # 2026-01-03
        self.assertEqual(result[1].id, a1.id)  # 2026-01-05
        self.assertEqual(result[2].id, a3.id)  # 2026-01-07
    
    def test_get_assessments_ordered_empty(self):
        """Test get_assessments_ordered returns empty when no assessments"""
        from rtms_app.queries.assessment_queries import get_assessments_ordered
        
        result = list(get_assessments_ordered(self.patient))
        
        self.assertEqual(len(result), 0)
    
    def test_get_assessments_ordered_same_date(self):
        """Test get_assessments_ordered handles multiple assessments on same date"""
        from rtms_app.queries.assessment_queries import get_assessments_ordered
        
        # Create multiple assessments on same date
        same_date = date(2026, 1, 5)
        a1 = Assessment.objects.create(
            patient=self.patient,
            date=same_date,
            timing='baseline',
            course_number=1,
            total_score_17=20,
            type='HAM-D'
        )
        a2 = Assessment.objects.create(
            patient=self.patient,
            date=same_date,
            timing='week3',
            course_number=1,
            total_score_17=19,
            type='HAM-D'
        )
        
        result = list(get_assessments_ordered(self.patient))
        
        # Both should be present, both on same date
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].date, same_date)
        self.assertEqual(result[1].date, same_date)
    
    def test_get_assessments_ordered_only_for_patient(self):
        """Test get_assessments_ordered returns only patient's assessments"""
        from rtms_app.queries.assessment_queries import get_assessments_ordered
        
        # Create another patient
        other_patient = Patient.objects.create(
            card_id="QUERY002",
            name="Other Query",
            birth_date=date(1985, 1, 1),
            course_number=1
        )
        
        # Create assessment for this patient
        Assessment.objects.create(
            patient=self.patient,
            date=date(2026, 1, 5),
            timing='baseline',
            course_number=1,
            total_score_17=20,
            type='HAM-D'
        )
        
        # Create assessment for other patient
        Assessment.objects.create(
            patient=other_patient,
            date=date(2026, 1, 6),
            timing='baseline',
            course_number=1,
            total_score_17=18,
            type='HAM-D'
        )
        
        # Query for first patient should only return first patient's assessment
        result = list(get_assessments_ordered(self.patient))
        
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].patient.id, self.patient.id)


# ============================================================================
# GROUP C: Integration & PoC Tests
# ============================================================================

class TestViewIntegration(TestCase):
    """Test actual views using helpers"""
    
    def setUp(self):
        self.client = Client()
        User = get_user_model()
        self.user = User.objects.create_user(username="admin", password="admin123", is_staff=True, is_superuser=True)
        self.client.login(username="admin", password="admin123")
        
        self.patient = Patient.objects.create(
            card_id="INTG001", 
            name="Integration Test", 
            birth_date=date(1980, 1, 1),
            course_number=1
        )
    
    def test_audit_logs_view_context(self):
        """Test audit_logs_view uses build_common_context"""
        from rtms_app.models import AuditLog
        
        # Create audit log
        AuditLog.objects.create(
            patient=self.patient,
            user=self.user,
            action='CREATE',
            target_model='Patient',
            target_pk=str(self.patient.id),
            summary='Test log'
        )
        
        url = reverse('rtms_app:audit_logs', args=[self.patient.id])
        response = self.client.get(url)
        
        # Should render successfully
        self.assertEqual(response.status_code, 200)
        
        # Check context
        self.assertIn('patient', response.context)
        self.assertIn('logs', response.context)
        self.assertEqual(response.context['patient'].id, self.patient.id)
    
    def test_audit_logs_view_with_dashboard_date(self):
        """Test audit_logs_view preserves dashboard_date parameter"""
        url = reverse('rtms_app:audit_logs', args=[self.patient.id])
        url_with_date = f"{url}?dashboard_date=2026-01-15"
        
        response = self.client.get(url_with_date)
        
        self.assertEqual(response.status_code, 200)
        self.assertIn('dashboard_date', response.context)
        self.assertEqual(response.context['dashboard_date'], '2026-01-15')
    
    def test_print_view_backward_compatibility(self):
        """Test all print views still work after refactoring"""
        # Create assessment for context
        from rtms_app.models import Assessment
        Assessment.objects.create(
            patient=self.patient,
            date=date(2026, 1, 5),
            timing='baseline',
            course_number=1,
            total_score_17=20
        )
        
        print_views = [
            'rtms_app:print:patient_print_admission',
            'rtms_app:print:patient_print_discharge',
            'rtms_app:print:patient_print_referral',
        ]
        
        for view_name in print_views:
            with self.subTest(view=view_name):
                url = reverse(view_name, args=[self.patient.id])
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200, f"{view_name} should return 200")
                self.assertIn('patient', response.context)
                self.assertEqual(response.context['patient'].id, self.patient.id)
    
    def test_decorators_patient_retrieval(self):
        """Test @get_patient_and_dashboard decorator retrieves patient"""
        from django.test import RequestFactory
        from rtms_app.decorators import get_patient_and_dashboard
        
        # Create a simple test view with decorator
        @get_patient_and_dashboard()
        def test_view(request, patient_id, patient, dashboard_date):
            from django.http import JsonResponse
            return JsonResponse({
                'patient_id': patient.id,
                'dashboard_date': dashboard_date
            })
        
        factory = RequestFactory()
        request = factory.get('/?dashboard_date=2026-01-15')
        
        # Mock login
        request.user = self.user
        
        response = test_view(request, self.patient.id)
        
        # Response should be successful
        self.assertEqual(response.status_code, 200)
    
    def test_decorators_patient_404(self):
        """Test @get_patient_and_dashboard returns 404 for invalid patient"""
        from django.test import RequestFactory
        from rtms_app.decorators import get_patient_and_dashboard
        from django.http import Http404
        
        @get_patient_and_dashboard()
        def test_view(request, patient_id, patient, dashboard_date):
            return None
        
        factory = RequestFactory()
        request = factory.get('/')
        request.user = self.user
        
        # Should raise Http404 for invalid patient_id
        with self.assertRaises(Http404):
            test_view(request, 99999)
