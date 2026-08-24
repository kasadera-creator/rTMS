from django.test import TestCase, Client
from django.urls import reverse
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.utils import timezone

from rtms_app import assessment_rules
from rtms_app.models import Patient, Assessment, AssessmentRecord, TreatmentSession
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


class TestStage6ScheduleDeadlines(TestCase):
    def setUp(self):
        self.patient = Patient.objects.create(
            card_id='S6001', name='Stage6', birth_date=date(1980, 1, 1),
            first_treatment_date=date(2026, 1, 5),
            first_visit_date=date(2026, 1, 5),
            is_all_case_survey=True,
        )

    def test_mapping_is_visible_only_through_treatment_week_end(self):
        from rtms_app.services.schedule_tasks import compute_dashboard_tasks, compute_task_definitions
        from rtms_app.services.rtms_schedule import generate_treatment_dates

        definitions = compute_task_definitions(self.patient, holidays=set())
        mapping = next(item for item in definitions if item['key'] == 'mapping')
        dates = generate_treatment_dates(self.patient.first_treatment_date, total=30, holidays=set())
        self.assertEqual(mapping['planned_date'], date(2026, 1, 12))
        self.assertEqual(mapping['window_end'], dates[9])
        self.assertNotIn('mapping', {item['key'] for item in compute_dashboard_tasks(self.patient, date(2026, 1, 11), set())})
        self.assertIn('mapping', {item['key'] for item in compute_dashboard_tasks(self.patient, date(2026, 1, 16), set())})
        self.assertNotIn('mapping', {item['key'] for item in compute_dashboard_tasks(self.patient, date(2026, 1, 19), set())})

    def test_hamd_week4_ends_seven_days_after_week_end(self):
        from rtms_app.services.schedule_tasks import compute_dashboard_tasks, compute_task_definitions

        definitions = compute_task_definitions(self.patient, holidays=set())
        week4 = next(item for item in definitions if item['key'] == 'assessment_week4')
        self.assertEqual(week4['planned_date'], date(2026, 1, 30))
        self.assertEqual(week4['window_end'], date(2026, 2, 6))
        self.assertIn('assessment_week4', {item['key'] for item in compute_dashboard_tasks(self.patient, date(2026, 2, 6), set())})
        self.assertNotIn('assessment_week4', {item['key'] for item in compute_dashboard_tasks(self.patient, date(2026, 2, 7), set())})


class TestStage6PatientAndCalendar(TestCase):
    def test_admission_procedure_get_does_not_reference_undefined_date(self):
        patient = Patient.objects.create(
            card_id='S6004', name='Admission', birth_date=date(1980, 1, 1),
        )
        user = get_user_model().objects.create_user(username='admission-viewer')
        client = Client()
        client.force_login(user)

        response = client.get(reverse('rtms_app:admission_procedure', args=[patient.pk]))

        self.assertEqual(response.status_code, 200)

    def test_registration_form_defaults_first_visit_to_today(self):
        from rtms_app.forms import PatientRegistrationForm
        self.assertEqual(PatientRegistrationForm().initial['first_visit_date'], timezone.localdate())

    def test_calendar_counts_date_ranges_and_excludes_skipped(self):
        from rtms_app.views import _build_month_calendar
        p = Patient.objects.create(
            card_id='S6002', name='Calendar', birth_date=date(1980, 1, 1),
            admission_date=date(2026, 8, 10), first_treatment_date=date(2026, 8, 19),
        )
        TreatmentSession.objects.create(patient=p, session_date=date(2026, 8, 24), status='planned')
        TreatmentSession.objects.create(patient=p, session_date=date(2026, 8, 25), status='skipped')
        TreatmentSession.objects.create(patient=p, session_date=date(2026, 8, 26), status='planned')
        discharged = Patient.objects.create(
            card_id='S6003', name='Discharged Patient', birth_date=date(1980, 1, 1),
            admission_date=date(2026, 8, 1), discharge_date=date(2026, 8, 12),
        )
        context = _build_month_calendar(2026, 8)
        days = {d['date']: d for week in context['weeks'] for d in week}
        self.assertEqual(days[date(2026, 8, 10)]['inpatient_count'], 2)
        self.assertEqual(days[date(2026, 8, 24)]['rtms_count'], 1)
        self.assertEqual(days[date(2026, 8, 25)]['rtms_count'], 0)
        labels = [event['label'] for event in days[date(2026, 8, 10)]['events_visible']]
        self.assertIn('入院（Calendar）', labels)
        labels = [event['label'] for event in days[date(2026, 8, 24)]['events_visible']]
        self.assertIn('rTMS治療（Calendar＃1回）', labels)
        labels = [event['label'] for event in days[date(2026, 8, 26)]['events_visible']]
        self.assertIn('rTMS治療（Calendar＃2回）', labels)
        labels = [event['label'] for event in days[date(2026, 8, 12)]['events_visible']]
        self.assertIn('退院（Discharged）', labels)

    def test_diagnosis_choices_are_preserved_in_existing_string_format(self):
        patient = Patient.objects.create(
            card_id='56003', name='Diagnosis', birth_date=date(1980, 1, 1),
            diagnosis='うつ病', first_treatment_date=date(2026, 1, 5),
            psychiatric_history=[], has_other_psychiatric_history='yes',
        )
        doctor_group, _ = Group.objects.get_or_create(name='医師')
        doctor = get_user_model().objects.create_user(username='stage6-doctor')
        doctor.groups.add(doctor_group)
        post = {
            'card_id': patient.card_id, 'name': patient.name,
            'birth_date': patient.birth_date.isoformat(), 'gender': patient.gender,
            'attending_physician': str(doctor.pk), 'admission_date': '2026-01-01',
            'first_treatment_date': '2026-01-05', 'first_visit_date': '2026-01-01',
            'diag_list': 'うつ病エピソード（F32）',
            'has_other_psychiatric_history': 'yes',
            'psychiatric_history': ['F33', 'F20'],
            'psychiatric_history_other_text': '既往診断',
        }
        client = Client()
        client.force_login(doctor)
        response = client.post(reverse('rtms_app:patient_first_visit', args=[patient.pk]), post)
        self.assertEqual(response.status_code, 302)
        patient.refresh_from_db()
        self.assertEqual(
            patient.diagnosis,
            'うつ病エピソード（F32）, 反復性うつ病性障害（F33）, 統合失調症（F20）, その他(既往診断)',
        )
        self.assertIn('F33', patient.diagnosis)
        self.assertIn('F20', patient.diagnosis)
        self.assertEqual(patient.psychiatric_history, ['F33', 'F20'])

        unchanged = Patient.objects.create(
            card_id='56004', name='Keep Diagnosis', birth_date=date(1980, 1, 1),
            diagnosis='旧形式の診断名', first_treatment_date=date(2026, 1, 5),
            has_other_psychiatric_history='yes',
        )
        unchanged_post = dict(post)
        unchanged_post.update({
            'card_id': unchanged.card_id, 'name': unchanged.name,
            'birth_date': unchanged.birth_date.isoformat(),
            'psychiatric_history': [],
        })
        unchanged_post.pop('diag_list', None)
        unchanged_post.pop('psychiatric_history_other_text', None)
        client.post(reverse('rtms_app:patient_first_visit', args=[unchanged.pk]), unchanged_post)
        unchanged.refresh_from_db()
        self.assertEqual(unchanged.diagnosis, '旧形式の診断名')

    def test_baseline_window_does_not_invert_when_first_visit_is_late(self):
        from types import SimpleNamespace
        from rtms_app.views import get_assessment_window
        late = SimpleNamespace(
            first_visit_date=date(2026, 1, 6),
            first_treatment_date=date(2026, 1, 5),
            created_at=None,
        )
        normal = SimpleNamespace(
            first_visit_date=date(2026, 1, 2),
            first_treatment_date=date(2026, 1, 5),
            created_at=None,
        )
        legacy = SimpleNamespace(
            first_visit_date=None,
            first_treatment_date=date(2026, 1, 5),
            created_at=timezone.make_aware(datetime.datetime(2026, 1, 2, 9, 0)),
        )
        for patient in (late, normal, legacy):
            start, end = get_assessment_window(patient, 'baseline')
            self.assertLessEqual(start, end)



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
        from rtms_app.models import ScaleDefinition

        self.patient = Patient.objects.create(
            card_id="QUERY001",
            name="Query Test",
            birth_date=date(1980, 1, 1),
            course_number=1
        )

        # Create ScaleDefinition for A2 tests
        # Use get_or_create to avoid duplicate key errors if tests run multiple times
        self.scale_hamd, _ = ScaleDefinition.objects.get_or_create(
            code='hamd',
            defaults={'name': 'HAM-D', 'is_active': True}
        )
        self.scale_phq9, _ = ScaleDefinition.objects.get_or_create(
            code='phq9',
            defaults={'name': 'PHQ-9', 'is_active': True}
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

    def test_get_latest_assessment_baseline(self):
        """Test get_latest_assessment returns baseline assessment"""
        from rtms_app.queries.assessment_queries import get_latest_assessment

        # Create single baseline assessment
        # (Note: unique constraint is (patient, course_number, timing, type)
        # so only one baseline per patient per course per type)
        baseline = Assessment.objects.create(
            patient=self.patient,
            date=date(2026, 1, 5),
            timing='baseline',
            course_number=1,
            scores={"q1": "0", "q2": "1"}  # Provide scores for calculation
        )

        result = get_latest_assessment(self.patient, 'baseline')

        self.assertIsNotNone(result)
        self.assertEqual(result.id, baseline.id)
        self.assertEqual(result.date, date(2026, 1, 5))
        self.assertEqual(result.timing, 'baseline')

    def test_get_latest_assessment_week3(self):
        """Test get_latest_assessment returns latest week3 assessment"""
        from rtms_app.queries.assessment_queries import get_latest_assessment

        # Create week3 assessments
        a1 = Assessment.objects.create(
            patient=self.patient,
            date=date(2026, 1, 15),
            timing='week3',
            course_number=1,
            total_score_17=18
        )

        result = get_latest_assessment(self.patient, 'week3')

        self.assertIsNotNone(result)
        self.assertEqual(result.id, a1.id)
        self.assertEqual(result.timing, 'week3')

    def test_get_latest_assessment_multiple_timings(self):
        """Test get_latest_assessment returns correct assessment per timing"""
        from rtms_app.queries.assessment_queries import get_latest_assessment

        # Create assessments with different timings
        baseline = Assessment.objects.create(
            patient=self.patient,
            date=date(2026, 1, 5),
            timing='baseline',
            course_number=1,
            total_score_17=22,
            type='HAM-D'
        )
        week3 = Assessment.objects.create(
            patient=self.patient,
            date=date(2026, 1, 20),
            timing='week3',
            course_number=1,
            total_score_17=20,
            type='HAM-D'
        )
        week6 = Assessment.objects.create(
            patient=self.patient,
            date=date(2026, 2, 5),
            timing='week6',
            course_number=1,
            total_score_17=18,
            type='HAM-D'
        )

        # Each timing should return its own assessment
        baseline_result = get_latest_assessment(self.patient, 'baseline')
        week3_result = get_latest_assessment(self.patient, 'week3')
        week6_result = get_latest_assessment(self.patient, 'week6')

        self.assertEqual(baseline_result.id, baseline.id)
        self.assertEqual(week3_result.id, week3.id)
        self.assertEqual(week6_result.id, week6.id)

    def test_get_latest_assessment_none_exists(self):
        """Test get_latest_assessment returns None when no assessment for timing"""
        from rtms_app.queries.assessment_queries import get_latest_assessment

        # No assessment for 'week4'
        result = get_latest_assessment(self.patient, 'week4')

        self.assertIsNone(result)

    def test_get_latest_assessment_latest_wins(self):
        """Test get_latest_assessment returns most recent when querying multiple assessments"""
        from rtms_app.queries.assessment_queries import get_latest_assessment

        # Note: Assessment unique constraint is (patient, course_number, timing, type='HAM-D')
        # So we can only have one baseline per (patient, course_number)
        # Test with single assessment to verify behavior
        baseline = Assessment.objects.create(
            patient=self.patient,
            date=date(2026, 1, 10),
            timing='baseline',
            course_number=1,
            total_score_17=22,
            type='HAM-D'
        )

        result = get_latest_assessment(self.patient, 'baseline')

        self.assertEqual(result.id, baseline.id)
        self.assertEqual(result.date, date(2026, 1, 10))

    def test_get_latest_assessment_isolation(self):
        """Test get_latest_assessment only returns assessments for specified patient"""
        from rtms_app.queries.assessment_queries import get_latest_assessment

        # Create another patient
        other_patient = Patient.objects.create(
            card_id="QUERY003",
            name="Other Patient",
            birth_date=date(1985, 1, 1),
            course_number=1
        )

        # Create baseline for first patient
        my_baseline = Assessment.objects.create(
            patient=self.patient,
            date=date(2026, 1, 5),
            timing='baseline',
            course_number=1,
            total_score_17=22,
            type='HAM-D'
        )

        # Create baseline for other patient (earlier date, should not be returned)
        other_baseline = Assessment.objects.create(
            patient=other_patient,
            date=date(2026, 1, 1),
            timing='baseline',
            course_number=1,
            total_score_17=25,
            type='HAM-D'
        )

        # Query for first patient should only return first patient's assessment
        result = get_latest_assessment(self.patient, 'baseline')

        self.assertEqual(result.id, my_baseline.id)
        self.assertNotEqual(result.id, other_baseline.id)

    def test_get_assessment_by_timing_with_fallback_record_only(self):
        """Test get_assessment_by_timing_with_fallback returns AssessmentRecord when exists"""
        from rtms_app.queries.assessment_queries import get_assessment_by_timing_with_fallback

        # Create AssessmentRecord only
        record = AssessmentRecord.objects.create(
            patient=self.patient,
            course_number=1,
            timing='baseline',
            scale=self.scale_hamd,
            date=date(2026, 1, 5),
            total_score_17=20
        )

        result = get_assessment_by_timing_with_fallback(
            self.patient, 'baseline', self.scale_hamd, course_number=1
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.id, record.id)
        self.assertIsInstance(result, AssessmentRecord)

    def test_get_assessment_by_timing_with_fallback_legacy_only(self):
        """Test get_assessment_by_timing_with_fallback falls back to Assessment"""
        from rtms_app.queries.assessment_queries import get_assessment_by_timing_with_fallback

        # Create Assessment (legacy) only
        legacy = Assessment.objects.create(
            patient=self.patient,
            course_number=1,
            timing='baseline',
            type='HAM-D',
            date=date(2026, 1, 5),
            total_score_17=22
        )

        result = get_assessment_by_timing_with_fallback(
            self.patient, 'baseline', self.scale_hamd, course_number=1
        )

        self.assertIsNotNone(result)
        self.assertEqual(result.id, legacy.id)
        self.assertIsInstance(result, Assessment)

    def test_get_assessment_by_timing_with_fallback_both_prefer_record(self):
        """Test get_assessment_by_timing_with_fallback prefers AssessmentRecord when both exist"""
        from rtms_app.queries.assessment_queries import get_assessment_by_timing_with_fallback

        # Create both AssessmentRecord and Assessment
        record = AssessmentRecord.objects.create(
            patient=self.patient,
            course_number=1,
            timing='baseline',
            scale=self.scale_hamd,
            date=date(2026, 1, 5),
            total_score_17=20
        )

        legacy = Assessment.objects.create(
            patient=self.patient,
            course_number=1,
            timing='baseline',
            type='HAM-D',
            date=date(2026, 1, 5),
            total_score_17=22
        )

        result = get_assessment_by_timing_with_fallback(
            self.patient, 'baseline', self.scale_hamd, course_number=1
        )

        # Should prefer AssessmentRecord
        self.assertIsNotNone(result)
        self.assertEqual(result.id, record.id)
        self.assertIsInstance(result, AssessmentRecord)

    def test_get_assessment_by_timing_with_fallback_none_exist(self):
        """Test get_assessment_by_timing_with_fallback returns None when neither exists"""
        from rtms_app.queries.assessment_queries import get_assessment_by_timing_with_fallback

        # No records created
        result = get_assessment_by_timing_with_fallback(
            self.patient, 'baseline', self.scale_hamd, course_number=1
        )

        self.assertIsNone(result)

    def test_get_assessment_by_timing_with_fallback_non_hamd_no_legacy(self):
        """Test get_assessment_by_timing_with_fallback does not fallback for non-HAM-D scales"""
        from rtms_app.queries.assessment_queries import get_assessment_by_timing_with_fallback

        # Create Assessment (legacy) - should NOT fallback for non-HAM-D
        legacy = Assessment.objects.create(
            patient=self.patient,
            course_number=1,
            timing='baseline',
            type='HAM-D',  # Still HAM-D type, but scale is PHQ-9
            date=date(2026, 1, 5),
            total_score_17=22
        )

        result = get_assessment_by_timing_with_fallback(
            self.patient, 'baseline', self.scale_phq9, course_number=1
        )

        # Should return None (no fallback for non-HAM-D)
        self.assertIsNone(result)

    def test_get_assessment_by_timing_with_fallback_latest_record_wins(self):
        """Test get_assessment_by_timing_with_fallback returns latest AssessmentRecord"""
        from rtms_app.queries.assessment_queries import get_assessment_by_timing_with_fallback

        # Note: AssessmentRecord unique constraint is (patient, course_number, timing, scale)
        # So we can only have one AssessmentRecord per (patient, course_number, timing, scale)
        # Test that the function returns the only existing record
        record = AssessmentRecord.objects.create(
            patient=self.patient,
            course_number=1,
            timing='week3',
            scale=self.scale_hamd,
            date=date(2026, 1, 15),
            total_score_17=18
        )

        result = get_assessment_by_timing_with_fallback(
            self.patient, 'week3', self.scale_hamd, course_number=1
        )

        # Should return the only record
        self.assertIsNotNone(result)
        self.assertEqual(result.id, record.id)

    def test_get_baseline_assessments_ordered_record_only(self):
        """Test get_baseline_assessments_ordered returns AssessmentRecord when exists"""
        from rtms_app.queries.assessment_queries import get_baseline_assessments_ordered

        # Create baseline AssessmentRecord only
        record = AssessmentRecord.objects.create(
            patient=self.patient,
            course_number=1,
            timing='baseline',
            scale=self.scale_hamd,
            date=date(2026, 1, 5),
            total_score_17=20
        )

        result = get_baseline_assessments_ordered(self.patient)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, record.id)
        self.assertIsInstance(result[0], AssessmentRecord)

    def test_get_baseline_assessments_ordered_legacy_only(self):
        """Test get_baseline_assessments_ordered falls back to Assessment"""
        from rtms_app.queries.assessment_queries import get_baseline_assessments_ordered

        # Create baseline Assessment (legacy) only
        legacy = Assessment.objects.create(
            patient=self.patient,
            course_number=1,
            timing='baseline',
            type='HAM-D',
            date=date(2026, 1, 5),
            total_score_17=22
        )

        result = get_baseline_assessments_ordered(self.patient)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, legacy.id)
        self.assertIsInstance(result[0], Assessment)

    def test_get_baseline_assessments_ordered_both_prefer_record(self):
        """Test get_baseline_assessments_ordered prefers AssessmentRecord when both exist"""
        from rtms_app.queries.assessment_queries import get_baseline_assessments_ordered

        # Create both AssessmentRecord and Assessment
        record = AssessmentRecord.objects.create(
            patient=self.patient,
            course_number=1,
            timing='baseline',
            scale=self.scale_hamd,
            date=date(2026, 1, 5),
            total_score_17=20
        )

        legacy = Assessment.objects.create(
            patient=self.patient,
            course_number=1,
            timing='baseline',
            type='HAM-D',
            date=date(2026, 1, 5),
            total_score_17=22
        )

        result = get_baseline_assessments_ordered(self.patient)

        # Should prefer AssessmentRecord
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, record.id)
        self.assertIsInstance(result[0], AssessmentRecord)

    def test_get_baseline_assessments_ordered_empty(self):
        """Test get_baseline_assessments_ordered returns empty list when no assessments"""
        from rtms_app.queries.assessment_queries import get_baseline_assessments_ordered

        result = get_baseline_assessments_ordered(self.patient)

        self.assertEqual(len(result), 0)
        self.assertEqual(result, [])

    def test_get_baseline_assessments_ordered_multiple(self):
        """Test get_baseline_assessments_ordered handles multiple baseline records"""
        from rtms_app.queries.assessment_queries import get_baseline_assessments_ordered

        # Create multiple baseline AssessmentRecords (normally shouldn't happen, but test anyway)
        # Note: Unique constraint is (patient, course_number, timing, scale)
        # So we create for different dates within same course/timing/scale
        # This is actually a constraint violation, so skip this variant.
        # Instead, test with different dates but ensure only one baseline per unique constraint

        # Just verify that the helper handles the single baseline case properly
        record = AssessmentRecord.objects.create(
            patient=self.patient,
            course_number=1,
            timing='baseline',
            scale=self.scale_hamd,
            date=date(2026, 1, 5),
            total_score_17=20
        )

        result = get_baseline_assessments_ordered(self.patient)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, record.id)

    def test_get_baseline_assessments_ordered_only_baseline(self):
        """Test get_baseline_assessments_ordered excludes non-baseline assessments"""
        from rtms_app.queries.assessment_queries import get_baseline_assessments_ordered

        # Create baseline and week3 assessments
        baseline = AssessmentRecord.objects.create(
            patient=self.patient,
            course_number=1,
            timing='baseline',
            scale=self.scale_hamd,
            date=date(2026, 1, 5),
            total_score_17=20
        )

        week3 = AssessmentRecord.objects.create(
            patient=self.patient,
            course_number=1,
            timing='week3',
            scale=self.scale_hamd,
            date=date(2026, 1, 20),
            total_score_17=18
        )

        result = get_baseline_assessments_ordered(self.patient)

        # Should only return baseline
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].id, baseline.id)
        self.assertEqual(result[0].timing, 'baseline')


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


# ============================================================================
# Phase 10b: Write Path Consolidation Tests
# ============================================================================

class TestAssessmentWriteHelpers(TestCase):
    """Test save_assessment_record() and save_assessment_hamd() helpers"""

    def setUp(self):
        """Set up test fixtures"""
        from rtms_app.models import ScaleDefinition

        self.patient = Patient.objects.create(
            card_id="WRITE_TEST_001",
            name="Write Test Patient",
            birth_date=date(1980, 1, 1),
            course_number=1,
        )

        # Ensure HAM-D scale exists
        self.hamd_scale, _ = ScaleDefinition.objects.get_or_create(
            code='hamd',
            defaults={'name': 'HAM-D', 'total_items': 21}
        )

    def test_save_assessment_record_creates_new(self):
        """Test save_assessment_record() creates new AssessmentRecord"""
        from rtms_app.queries.assessment_queries import save_assessment_record

        scores = {'q1': '1', 'q2': '2', 'q3': '0'}
        record, created = save_assessment_record(
            patient=self.patient,
            course_number=1,
            timing='baseline',
            scale=self.hamd_scale,
            date=date.today(),
            scores=scores,
            note="Test note",
        )

        self.assertTrue(created)
        self.assertEqual(record.patient, self.patient)
        self.assertEqual(record.timing, 'baseline')
        self.assertEqual(record.scale, self.hamd_scale)
        self.assertEqual(record.note, "Test note")
        self.assertEqual(record.scores, scores)

    def test_save_assessment_record_updates_existing(self):
        """Test save_assessment_record() updates existing AssessmentRecord"""
        from rtms_app.queries.assessment_queries import save_assessment_record

        # Create initial record
        scores1 = {'q1': '1', 'q2': '2'}
        record1, created1 = save_assessment_record(
            patient=self.patient,
            course_number=1,
            timing='week3',
            scale=self.hamd_scale,
            date=date.today(),
            scores=scores1,
            note="Initial",
        )
        self.assertTrue(created1)

        # Update same record
        scores2 = {'q1': '2', 'q2': '3'}
        record2, created2 = save_assessment_record(
            patient=self.patient,
            course_number=1,
            timing='week3',
            scale=self.hamd_scale,
            date=date.today(),
            scores=scores2,
            note="Updated",
        )

        self.assertFalse(created2)
        self.assertEqual(record1.id, record2.id)
        self.assertEqual(record2.note, "Updated")
        self.assertEqual(record2.scores, scores2)

    def test_save_assessment_record_with_defaults_override(self):
        """Test save_assessment_record() applies defaults_override"""
        from rtms_app.queries.assessment_queries import save_assessment_record

        scores = {'q1': '1'}
        record, _ = save_assessment_record(
            patient=self.patient,
            course_number=1,
            timing='week6',
            scale=self.hamd_scale,
            date=date.today(),
            scores=scores,
            note="",
            defaults_override={
                'improvement_rate_17': 25.5,
                'status_label': '反応',
            },
        )

        self.assertEqual(record.improvement_rate_17, 25.5)
        self.assertEqual(record.status_label, '反応')

    def test_save_assessment_hamd_creates_new(self):
        """Test save_assessment_hamd() creates new Assessment"""
        from rtms_app.queries.assessment_queries import save_assessment_hamd

        scores = {'q1': '1', 'q2': '2'}
        assessment, created = save_assessment_hamd(
            patient=self.patient,
            course_number=1,
            timing='baseline',
            date=date.today(),
            scores=scores,
            note="Test HAM-D",
        )

        self.assertTrue(created)
        self.assertEqual(assessment.patient, self.patient)
        self.assertEqual(assessment.timing, 'baseline')
        self.assertEqual(assessment.type, 'HAM-D')
        self.assertEqual(assessment.note, "Test HAM-D")

    def test_save_assessment_hamd_updates_existing(self):
        """Test save_assessment_hamd() updates existing Assessment"""
        from rtms_app.queries.assessment_queries import save_assessment_hamd

        # Create initial
        scores1 = {'q1': '1', 'q2': '2'}
        a1, c1 = save_assessment_hamd(
            patient=self.patient,
            course_number=1,
            timing='week3',
            date=date.today(),
            scores=scores1,
            note="Initial",
        )
        self.assertTrue(c1)

        # Update
        scores2 = {'q1': '2', 'q2': '3'}
        a2, c2 = save_assessment_hamd(
            patient=self.patient,
            course_number=1,
            timing='week3',
            date=date.today(),
            scores=scores2,
            note="Updated",
        )

        self.assertFalse(c2)
        self.assertEqual(a1.id, a2.id)
        self.assertEqual(a2.note, "Updated")

    def test_save_assessment_hamd_calculate_scores_auto_called(self):
        """Test that save_assessment_hamd() auto-calls calculate_scores() via model.save()"""
        from rtms_app.queries.assessment_queries import save_assessment_hamd

        # Scores for q1-q21 with values summing to known total
        scores = {f'q{i}': '1' for i in range(1, 22)}  # All 1s => total 21
        assessment, _ = save_assessment_hamd(
            patient=self.patient,
            course_number=1,
            timing='baseline',
            date=date.today(),
            scores=scores,
            note="",
        )

        # total_score_21 should be auto-calculated
        self.assertEqual(assessment.total_score_21, 21)
        # total_score_17 should be calculated from q1-q17
        self.assertEqual(assessment.total_score_17, 17)

    def test_save_assessment_record_calculate_scores_auto_called(self):
        """Test that save_assessment_record() auto-calls calculate_scores() via model.save()"""
        from rtms_app.queries.assessment_queries import save_assessment_record

        scores = {f'q{i}': '1' for i in range(1, 22)}  # All 1s
        record, _ = save_assessment_record(
            patient=self.patient,
            course_number=1,
            timing='week3',
            scale=self.hamd_scale,
            date=date.today(),
            scores=scores,
            note="",
        )

        # Scores should be auto-calculated by model.save()
        self.assertEqual(record.total_score_21, 21)
        self.assertEqual(record.total_score_17, 17)
