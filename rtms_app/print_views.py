from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.http import HttpResponseNotAllowed
from urllib.parse import urlencode

from .models import Patient, Assessment, ConsentDocument, TreatmentSession, SideEffectCheck
from .views import generate_calendar_weeks

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
	return render(request, 'rtms_app/print/bundle.html', context)


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
	return render(request, 'rtms_app/print/path.html', context)


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
	return render(request, 'rtms_app/print/discharge_summary.html', context)


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
	return render(request, 'rtms_app/print/admission_summary.html', context)


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
	return render(request, 'rtms_app/print/referral.html', context)


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
	return render(request, 'rtms_app/print/suitability_questionnaire.html', context)


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
	return render(request, 'rtms_app/print/side_effect_check.html', context)
