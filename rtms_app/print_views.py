from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.http import HttpResponseNotAllowed
from urllib.parse import urlencode

from .models import Patient, Assessment, ConsentDocument, TreatmentSession, SideEffectCheck
from .views import generate_calendar_weeks
from .services.print_service import build_pdf_filename, CONTENT_LABELS
from django.template.loader import render_to_string
from django.http import HttpResponse


# Helper to provide HAMD trend columns with graceful fallback
def _hamd_cols_for_patient(patient):
	try:
		from .services.course_summary_service import build_assessment_trend
		cols = build_assessment_trend(patient)
		if cols:
			return cols
	except Exception:
		pass
	# fallback placeholder with four timings
	labels = [('baseline', '治療前'), ('week3', '3週'), ('week4', '4週'), ('week6', '6週')]
	return [
		{'timing': code, 'label': label, 'date_str': '-', 'hamd21': None, 'hamd17': None, 'improvement_pct_17': None, 'status_label': ''}
		for code, label in labels
	]

try:
	from weasyprint import HTML, CSS
	HAVE_WEASY = True
except Exception:
	HAVE_WEASY = False


def render_pdf_response(request, template, context, filename):
	# Render template fragment (use include_mode to avoid toolbar/wrappers)
	context = dict(context)
	context['include_mode'] = True
	html = render_to_string(template, context, request=request)
	if HAVE_WEASY:
		base_url = request.build_absolute_uri('/')
		pdf = HTML(string=html, base_url=base_url).write_pdf(stylesheets=[])
		resp = HttpResponse(pdf, content_type='application/pdf')
		# inline so browser opens PDF (user can save or print)
		resp['Content-Disposition'] = f'inline; filename="{filename}"'
		return resp
	else:
		# Fallback: return HTML so users can still view/print; warn in console
		return HttpResponse(html)

# map doc keys to templates or pdf statics
DOC_TEMPLATES = {
	"admission": {"label": "入院時サマリ", "template": "rtms_app/print/admission_summary.html"},
	"suitability": {"label": "rTMS問診票", "template": "rtms_app/print/suitability_questionnaire.html"},
	"consent_pdf": {"label": "説明同意書（標準）", "pdf_static": "rtms_app/docs/consent_default.pdf"},
	"discharge": {"label": "退院時サマリー", "template": "rtms_app/print/discharge_summary.html"},
	"referral": {"label": "紹介状", "template": "rtms_app/print/referral.html"},
	"path": {"label": "臨床経過表", "template": "rtms_app/print/path.html"},
}


@login_required
def patient_print_bundle(request, patient_id):
	patient = get_object_or_404(Patient, pk=patient_id)
	questionnaire = patient.questionnaire_data or {}
	assessments = Assessment.objects.filter(patient=patient, timing='baseline').order_by('date')

	# always use getlist to collect multiple docs from ?docs=...&docs=...
	docs = request.GET.getlist("docs")
	docs = [d for d in docs if d]

	# filter to allowed templates
	valid_docs = [d for d in docs if d in DOC_TEMPLATES]

	# build render list
	docs_to_render = []
	for key in valid_docs:
		tpl_info = DOC_TEMPLATES.get(key, {})
		label = tpl_info.get('label') or key
		if key == 'consent_pdf':
			# prefer uploaded consent if exists, otherwise fallback to static PDF
			doc = ConsentDocument.objects.order_by('-uploaded_at').first()
			if doc and getattr(doc, 'file', None):
				docs_to_render.append({'label': '説明同意書（最新）', 'pdf_url': doc.file.url})
			else:
				docs_to_render.append({'label': label, 'pdf_static': tpl_info.get('pdf_static')})
		else:
			if tpl_info.get('template'):
				docs_to_render.append({'label': label, 'template': tpl_info.get('template')})
			else:
				docs_to_render.append({'label': label})

	# determine a sensible back_url: explicit, Referer, or patient summary
	back_url = request.GET.get('back_url') or request.META.get('HTTP_REFERER') or reverse('rtms_app:patient_home', args=[patient.id])

	# if no valid docs selected, render bundle with an error message (don't 500)
	if not docs_to_render:
		context = {
			'patient': patient,
			'today': timezone.now().date(),
			'docs_to_render': [],
			'doc_templates': DOC_TEMPLATES,
			'bundle_error': '表示する文書がありません。チェックボックスを選択してください。',
			'back_url': back_url,
		}
		return render(request, 'rtms_app/print/bundle.html', context, status=400)

	context = {
		'patient': patient,
		'questionnaire': questionnaire,
		'assessments': assessments,
		'today': timezone.now().date(),
		'docs_to_render': docs_to_render,
		'doc_templates': DOC_TEMPLATES,
		'back_url': back_url,
	}
	# build a default pdf filename for the bundle (uses first doc label if available)
	first_label = docs_to_render[0]['label'] if docs_to_render else 'bundle'
	context['pdf_filename'] = build_pdf_filename(patient, getattr(patient, 'course_number', 1), first_label, timezone.now().date())
	# provide hamd_trend_cols to included print templates (e.g., discharge/referral)
	context['hamd_trend_cols'] = _hamd_cols_for_patient(patient)
	return render(request, 'rtms_app/print/bundle.html', context)


@login_required
def patient_print_bundle_pdf(request, patient_id):
	# Build same context as patient_print_bundle and render PDF
	patient = get_object_or_404(Patient, pk=patient_id)
	questionnaire = patient.questionnaire_data or {}
	assessments = Assessment.objects.filter(patient=patient, timing='baseline').order_by('date')

	docs = request.GET.getlist("docs")
	docs = [d for d in docs if d]
	valid_docs = [d for d in docs if d in DOC_TEMPLATES]
	docs_to_render = []
	for key in valid_docs:
		tpl_info = DOC_TEMPLATES.get(key, {})
		label = tpl_info.get('label') or key
		if key == 'consent_pdf':
			doc = ConsentDocument.objects.order_by('-uploaded_at').first()
			if doc and getattr(doc, 'file', None):
				docs_to_render.append({'label': '説明同意書（最新）', 'pdf_url': doc.file.url})
			else:
				docs_to_render.append({'label': label, 'pdf_static': tpl_info.get('pdf_static')})
		else:
			if tpl_info.get('template'):
				docs_to_render.append({'label': label, 'template': tpl_info.get('template')})
			else:
				docs_to_render.append({'label': label})

	back_url = request.GET.get('back_url') or request.META.get('HTTP_REFERER') or reverse('rtms_app:patient_home', args=[patient.id])

	if not docs_to_render:
		context = {
			'patient': patient,
			'today': timezone.now().date(),
			'docs_to_render': [],
			'doc_templates': DOC_TEMPLATES,
			'bundle_error': '表示する文書がありません。チェックボックスを選択してください。',
			'back_url': back_url,
		}
		return render_pdf_response(request, 'rtms_app/print/bundle.html', context, build_pdf_filename(patient, getattr(patient, 'course_number', 1), 'bundle', timezone.now().date()))

	context = {
		'patient': patient,
		'questionnaire': questionnaire,
		'assessments': assessments,
		'today': timezone.now().date(),
		'docs_to_render': docs_to_render,
		'doc_templates': DOC_TEMPLATES,
		'back_url': back_url,
	}
	first_label = docs_to_render[0]['label'] if docs_to_render else 'bundle'
	context['pdf_filename'] = build_pdf_filename(patient, getattr(patient, 'course_number', 1), first_label, timezone.now().date())
	# include hamd_trend_cols for PDF rendering as well
	context['hamd_trend_cols'] = _hamd_cols_for_patient(patient)
	return render_pdf_response(request, 'rtms_app/print/bundle.html', context, context['pdf_filename'])


@login_required
def print_clinical_path(request, patient_id):
	patient = get_object_or_404(Patient, pk=patient_id)
	calendar_weeks, assessment_events = generate_calendar_weeks(patient)
	back_url = request.GET.get('back_url') or request.META.get('HTTP_REFERER') or reverse('rtms_app:patient_home', args=[patient.id])
	context = {
		'patient': patient,
		'calendar_weeks': calendar_weeks,
		'assessment_events': assessment_events,
		'today': timezone.now().date(),
		'back_url': back_url,
	}
	# pdf filename
	context['pdf_filename'] = build_pdf_filename(patient, getattr(patient, 'course_number', 1), CONTENT_LABELS.get('path','臨床経過表'), timezone.now().date())
	return render(request, 'rtms_app/print/path.html', context)


@login_required
def print_clinical_path_pdf(request, patient_id):
	patient = get_object_or_404(Patient, pk=patient_id)
	calendar_weeks, assessment_events = generate_calendar_weeks(patient)
	back_url = request.GET.get('back_url') or request.META.get('HTTP_REFERER') or reverse('rtms_app:patient_home', args=[patient.id])
	context = {
		'patient': patient,
		'calendar_weeks': calendar_weeks,
		'assessment_events': assessment_events,
		'today': timezone.now().date(),
		'back_url': back_url,
	}
	context['pdf_filename'] = build_pdf_filename(patient, getattr(patient, 'course_number', 1), CONTENT_LABELS.get('path','臨床経過表'), timezone.now().date())
	return render_pdf_response(request, 'rtms_app/print/path.html', context, context['pdf_filename'])


@login_required
def patient_print_discharge(request, patient_id):
	patient = get_object_or_404(Patient, pk=patient_id)
	back_url = request.GET.get('back_url') or request.META.get('HTTP_REFERER') or reverse('rtms_app:patient_home', args=[patient.id])
    
	# Get assessments and collapse to latest per date
	assessments_qs = Assessment.objects.filter(patient=patient).order_by('date')
	latest_by_date = {}
	for a in assessments_qs:
		latest_by_date[a.date] = a
	test_scores = [latest_by_date[d] for d in sorted(latest_by_date.keys())]
	
	context = {
		'patient': patient,
		'today': timezone.now().date(),
		'test_scores': test_scores,
		'back_url': back_url,
	}
	# build hamd trend cols using shared service so print matches screen
	context['hamd_trend_cols'] = _hamd_cols_for_patient(patient)
	context['pdf_filename'] = build_pdf_filename(patient, getattr(patient, 'course_number', 1), CONTENT_LABELS.get('discharge','退院時サマリー'), timezone.now().date())
	return render(request, 'rtms_app/print/discharge_summary.html', context)


@login_required
def patient_print_discharge_pdf(request, patient_id):
	patient = get_object_or_404(Patient, pk=patient_id)
	back_url = request.GET.get('back_url') or request.META.get('HTTP_REFERER') or reverse('rtms_app:patient_home', args=[patient.id])
	assessments_qs = Assessment.objects.filter(patient=patient).order_by('date')
	latest_by_date = {}
	for a in assessments_qs:
		latest_by_date[a.date] = a
	test_scores = [latest_by_date[d] for d in sorted(latest_by_date.keys())]
	context = {
		'patient': patient,
		'today': timezone.now().date(),
		'test_scores': test_scores,
		'back_url': back_url,
	}
	# build hamd trend cols for PDF as well
	context['hamd_trend_cols'] = _hamd_cols_for_patient(patient)
	context['pdf_filename'] = build_pdf_filename(patient, getattr(patient, 'course_number', 1), CONTENT_LABELS.get('discharge','退院時サマリー'), timezone.now().date())
	return render_pdf_response(request, 'rtms_app/print/discharge_summary.html', context, context['pdf_filename'])


@login_required
def patient_print_admission(request, patient_id):
	from datetime import timedelta
	patient = get_object_or_404(Patient, pk=patient_id)
	back_url = request.GET.get('back_url') or request.META.get('HTTP_REFERER') or reverse('rtms_app:patient_home', args=[patient.id])
	
	# Get baseline assessments
	assessments = Assessment.objects.filter(patient=patient, timing='baseline').order_by('date')
	
	# Calculate estimated end date (30 sessions from first treatment date)
	end_date_est = None
	if patient.first_treatment_date:
		end_date_est = patient.first_treatment_date + timedelta(days=42)  # ~30 weekdays
	
	context = {
		'patient': patient,
		'today': timezone.now().date(),
		'assessments': assessments,
		'end_date_est': end_date_est,
		'back_url': back_url,
	}
	context['pdf_filename'] = build_pdf_filename(patient, getattr(patient, 'course_number', 1), CONTENT_LABELS.get('admission','入院時サマリ'), timezone.now().date())
	return render(request, 'rtms_app/print/admission_summary.html', context)


@login_required
def patient_print_admission_pdf(request, patient_id):
	from datetime import timedelta
	patient = get_object_or_404(Patient, pk=patient_id)
	back_url = request.GET.get('back_url') or request.META.get('HTTP_REFERER') or reverse('rtms_app:patient_home', args=[patient.id])
	assessments = Assessment.objects.filter(patient=patient, timing='baseline').order_by('date')
	end_date_est = None
	if patient.first_treatment_date:
		end_date_est = patient.first_treatment_date + timedelta(days=42)
	context = {
		'patient': patient,
		'today': timezone.now().date(),
		'assessments': assessments,
		'end_date_est': end_date_est,
		'back_url': back_url,
	}
	context['pdf_filename'] = build_pdf_filename(patient, getattr(patient, 'course_number', 1), CONTENT_LABELS.get('admission','入院時サマリ'), timezone.now().date())
	return render_pdf_response(request, 'rtms_app/print/admission_summary.html', context, context['pdf_filename'])


@login_required
def patient_print_referral(request, patient_id):
	patient = get_object_or_404(Patient, pk=patient_id)
	back_url = request.GET.get('back_url') or request.META.get('HTTP_REFERER') or reverse('rtms_app:patient_home', args=[patient.id])
	# 重複があれば同日最新のみ
	assessments_qs = Assessment.objects.filter(patient=patient).order_by('date')
	latest_by_date = {}
	for a in assessments_qs:
		latest_by_date[a.date] = a
	test_scores = [latest_by_date[d] for d in sorted(latest_by_date.keys())]
	context = {
		'patient': patient,
		'today': timezone.now().date(),
		'test_scores': test_scores,
		'back_url': back_url,
	}
	context['pdf_filename'] = build_pdf_filename(patient, getattr(patient, 'course_number', 1), CONTENT_LABELS.get('referral','紹介状'), timezone.now().date())
	return render(request, 'rtms_app/print/referral.html', context)


@login_required
def patient_print_referral_pdf(request, patient_id):
	patient = get_object_or_404(Patient, pk=patient_id)
	back_url = request.GET.get('back_url') or request.META.get('HTTP_REFERER') or reverse('rtms_app:patient_home', args=[patient.id])
	assessments_qs = Assessment.objects.filter(patient=patient).order_by('date')
	latest_by_date = {}
	for a in assessments_qs:
		latest_by_date[a.date] = a
	test_scores = [latest_by_date[d] for d in sorted(latest_by_date.keys())]
	context = {
		'patient': patient,
		'today': timezone.now().date(),
		'test_scores': test_scores,
		'back_url': back_url,
	}
	context['pdf_filename'] = build_pdf_filename(patient, getattr(patient, 'course_number', 1), CONTENT_LABELS.get('referral','紹介状'), timezone.now().date())
	return render_pdf_response(request, 'rtms_app/print/referral.html', context, context['pdf_filename'])


@login_required
def patient_print_suitability(request, patient_id):
	patient = get_object_or_404(Patient, pk=patient_id)
	questionnaire = patient.questionnaire_data or {}
	assessments = Assessment.objects.filter(patient=patient, timing='baseline').order_by('date')
	back_url = request.GET.get('back_url') or request.META.get('HTTP_REFERER') or reverse('rtms_app:patient_first_visit', args=[patient.id])
	context = {
		'patient': patient,
		'questionnaire': questionnaire,
		'assessments': assessments,
		'today': timezone.now().date(),
		'back_url': back_url,
	}
	context['pdf_filename'] = build_pdf_filename(patient, getattr(patient, 'course_number', 1), CONTENT_LABELS.get('suitability','rTMS問診票'), timezone.now().date())
	return render(request, 'rtms_app/print/suitability_questionnaire.html', context)


@login_required
def patient_print_suitability_pdf(request, patient_id):
	patient = get_object_or_404(Patient, pk=patient_id)
	questionnaire = patient.questionnaire_data or {}
	assessments = Assessment.objects.filter(patient=patient, timing='baseline').order_by('date')
	back_url = request.GET.get('back_url') or request.META.get('HTTP_REFERER') or reverse('rtms_app:patient_first_visit', args=[patient.id])
	context = {
		'patient': patient,
		'questionnaire': questionnaire,
		'assessments': assessments,
		'today': timezone.now().date(),
		'back_url': back_url,
	}
	context['pdf_filename'] = build_pdf_filename(patient, getattr(patient, 'course_number', 1), CONTENT_LABELS.get('suitability','rTMS問診票'), timezone.now().date())
	return render_pdf_response(request, 'rtms_app/print/suitability_questionnaire.html', context, context['pdf_filename'])


@login_required
def print_side_effect_check(request, patient_id, session_id):
	"""Print view for side-effect check of a specific treatment session."""
	if request.method != 'GET':
		return HttpResponseNotAllowed(['GET'])

	patient = get_object_or_404(Patient, pk=patient_id)
	session = get_object_or_404(TreatmentSession, pk=session_id, patient=patient)
	
	# Calculate session number (all treatment sessions for this patient up to and including this one)
	if getattr(session, 'session_date', None):
		session_number = TreatmentSession.objects.filter(
			patient=patient,
			session_date__lte=session.session_date,
		).order_by('session_date', 'date').count()
	else:
		session_number = TreatmentSession.objects.filter(
			patient=patient,
			date__lte=session.date,
		).order_by('date').count()
	
	# Get side-effect check if exists
	def default_rows():
		return [
			{"item": "頭皮痛・刺激痛", "before": 0, "during": 0, "after": 0, "relatedness": 0, "memo": ""},
			{"item": "顔面の不快感", "before": 0, "during": 0, "after": 0, "relatedness": 0, "memo": ""},
			{"item": "頸部痛・肩こり", "before": 0, "during": 0, "after": 0, "relatedness": 0, "memo": ""},
			{"item": "頭痛 (刺激後)", "before": 0, "during": 0, "after": 0, "relatedness": 0, "memo": ""},
			{"item": "けいれん (部位・時間)", "before": 0, "during": 0, "after": 0, "relatedness": 0, "memo": ""},
			{"item": "失神", "before": 0, "during": 0, "after": 0, "relatedness": 0, "memo": ""},
			{"item": "聴覚障害", "before": 0, "during": 0, "after": 0, "relatedness": 0, "memo": ""},
			{"item": "めまい・耳鳴り", "before": 0, "during": 0, "after": 0, "relatedness": 0, "memo": ""},
			{"item": "注意集中困難", "before": 0, "during": 0, "after": 0, "relatedness": 0, "memo": ""},
			{"item": "急性気分変化 (躁転など)", "before": 0, "during": 0, "after": 0, "relatedness": 0, "memo": ""},
			{"item": "その他", "before": 0, "during": 0, "after": 0, "relatedness": 0, "memo": ""},
		]

	try:
		side_effect_check = SideEffectCheck.objects.get(session=session)
		rows = side_effect_check.rows or default_rows()
		memo = side_effect_check.memo or ""
		signature = side_effect_check.physician_signature or ""
	except SideEffectCheck.DoesNotExist:
		rows = default_rows()
		memo = ""
		signature = ""
	
	# Always prefer explicit back_url (PRG). Fallback is deterministic treatment_add URL.
	back_url = request.GET.get('back_url')
	if not back_url:
		query = {}
		if getattr(session, 'session_date', None):
			query['date'] = session.session_date.isoformat()
		elif getattr(session, 'date', None):
			query['date'] = session.date.date().isoformat()
		dashboard_date = request.GET.get('dashboard_date')
		if dashboard_date:
			query['dashboard_date'] = dashboard_date
		base = reverse('rtms_app:treatment_add', args=[patient.id])
		back_url = f"{base}?{urlencode(query)}" if query else base
	
	context = {
		'patient': patient,
		'session': session,
		'session_number': session_number,
		'side_effect_rows': rows,
		'side_effect_memo': memo,
		'side_effect_signature': signature,
		'today': timezone.now().date(),
		'back_url': back_url,
	}
	# Prepare pdf filename
	try:
		target_date = session.session_date if getattr(session, 'session_date', None) else (session.date.date() if getattr(session, 'date', None) else timezone.now().date())
	except Exception:
		target_date = timezone.now().date()
	content_label = CONTENT_LABELS.get('side_effect', '治療実施記録票')
	context['pdf_filename'] = build_pdf_filename(patient, getattr(session, 'course_number', None) or getattr(patient, 'course_number', None), content_label, target_date)
	return render(request, 'rtms_app/print/side_effect_check.html', context)


@login_required
def print_side_effect_check_pdf(request, patient_id, session_id):
	if request.method != 'GET':
		return HttpResponseNotAllowed(['GET'])

	patient = get_object_or_404(Patient, pk=patient_id)
	session = get_object_or_404(TreatmentSession, pk=session_id, patient=patient)

	if getattr(session, 'session_date', None):
		session_number = TreatmentSession.objects.filter(
			patient=patient,
			session_date__lte=session.session_date,
		).order_by('session_date', 'date').count()
	else:
		session_number = TreatmentSession.objects.filter(
			patient=patient,
			date__lte=session.date,
		).order_by('date').count()

	def default_rows():
		return [
			{"item": "頭皮痛・刺激痛", "before": 0, "during": 0, "after": 0, "relatedness": 0, "memo": ""},
			{"item": "顔面の不快感", "before": 0, "during": 0, "after": 0, "relatedness": 0, "memo": ""},
			{"item": "頸部痛・肩こり", "before": 0, "during": 0, "after": 0, "relatedness": 0, "memo": ""},
			{"item": "頭痛 (刺激後)", "before": 0, "during": 0, "after": 0, "relatedness": 0, "memo": ""},
			{"item": "けいれん (部位・時間)", "before": 0, "during": 0, "after": 0, "relatedness": 0, "memo": ""},
			{"item": "失神", "before": 0, "during": 0, "after": 0, "relatedness": 0, "memo": ""},
			{"item": "聴覚障害", "before": 0, "during": 0, "after": 0, "relatedness": 0, "memo": ""},
			{"item": "めまい・耳鳴り", "before": 0, "during": 0, "after": 0, "relatedness": 0, "memo": ""},
			{"item": "注意集中困難", "before": 0, "during": 0, "after": 0, "relatedness": 0, "memo": ""},
			{"item": "急性気分変化 (躁転など)", "before": 0, "during": 0, "after": 0, "relatedness": 0, "memo": ""},
			{"item": "その他", "before": 0, "during": 0, "after": 0, "relatedness": 0, "memo": ""},
		]

	try:
		side_effect_check = SideEffectCheck.objects.get(session=session)
		rows = side_effect_check.rows or default_rows()
		memo = side_effect_check.memo or ""
		signature = side_effect_check.physician_signature or ""
	except SideEffectCheck.DoesNotExist:
		rows = default_rows()
		memo = ""
		signature = ""

	back_url = request.GET.get('back_url')
	if not back_url:
		query = {}
		if getattr(session, 'session_date', None):
			query['date'] = session.session_date.isoformat()
		elif getattr(session, 'date', None):
			query['date'] = session.date.date().isoformat()
		dashboard_date = request.GET.get('dashboard_date')
		if dashboard_date:
			query['dashboard_date'] = dashboard_date
		base = reverse('rtms_app:treatment_add', args=[patient.id])
		back_url = f"{base}?{urlencode(query)}" if query else base

	context = {
		'patient': patient,
		'session': session,
		'session_number': session_number,
		'side_effect_rows': rows,
		'side_effect_memo': memo,
		'side_effect_signature': signature,
		'today': timezone.now().date(),
		'back_url': back_url,
	}
	try:
		target_date = session.session_date if getattr(session, 'session_date', None) else (session.date.date() if getattr(session, 'date', None) else timezone.now().date())
	except Exception:
		target_date = timezone.now().date()
	content_label = CONTENT_LABELS.get('side_effect', '治療実施記録票')
	context['pdf_filename'] = build_pdf_filename(patient, getattr(session, 'course_number', None) or getattr(patient, 'course_number', None), content_label, target_date)
	return render_pdf_response(request, 'rtms_app/print/side_effect_check.html', context, context['pdf_filename'])
