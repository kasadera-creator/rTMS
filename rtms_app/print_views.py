from django.shortcuts import render, get_object_or_404, redirect
from django.urls import reverse
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.http import HttpResponseNotAllowed
from urllib.parse import urlencode
import logging

from .models import Patient, Assessment, ConsentDocument, TreatmentSession, SideEffectCheck
from .views import generate_calendar_weeks

# map doc keys to templates or pdf statics
DOC_TEMPLATES = {
	"admission": {"label": "入院時サマリ", "template": "rtms_app/print/admission_summary.html"},
	"suitability": {"label": "rTMS問診票", "template": "rtms_app/print/suitability_questionnaire.html"},
	"consent_pdf": {"label": "説明同意書（標準）", "pdf_static": "rtms_app/docs/consent_default.pdf"},
	"discharge": {"label": "退院時サマリー", "template": "rtms_app/print/discharge_summary.html"},
	"hamd_detail": {"label": "HAMD詳細票", "template": "rtms_app/print/hamd_detail.html"},
	"referral": {"label": "紹介状", "template": "rtms_app/print/referral.html"},
	"path": {"label": "臨床経過表", "template": "rtms_app/print/path.html"},
}

logger = logging.getLogger(__name__)


@login_required
def patient_print_bundle(request, patient_id):
	patient = get_object_or_404(Patient, pk=patient_id)
	questionnaire = patient.questionnaire_data or {}
	# 全期間の評価を使用（baseline限定ではなく）
	assessments = Assessment.objects.filter(patient=patient).order_by('date')

	# always use getlist to collect multiple docs from ?docs=...&docs=...
	docs = request.GET.getlist("docs")
	docs = [d for d in docs if d]
	logger.info("print bundle docs=%s", docs)

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

	# HAMD trend columns with evaluation (severity, improvement, response)
	def _severity_label_17(score:
						   int | None):
		if score is None:
			return None
		if score <= 7:
			return "正常"
		if score <= 13:
			return "軽症"
		if score <= 18:
			return "中等症"
		if score <= 22:
			return "重症"
		return "最重症"

	def _response_label(timing: str, ham17: int | None, imp: float | None):
		if timing == "baseline":
			return None
		if ham17 is not None and ham17 <= 7:
			return "寛解（中止/漸減）" if timing == "week3" else "寛解"
		if imp is not None and imp < 20.0:
			return "無効（中止）" if timing == "week3" else "無効"
		return "継続"

	timing_order = [("baseline", "治療前"), ("week3", "3週"), ("week4", "4週"), ("week6", "6週")]
	latest_by_timing = {t: Assessment.objects.filter(patient=patient, timing=t).order_by("-date").first() for t, _ in timing_order}
	baseline_obj = latest_by_timing.get("baseline")
	baseline_17 = getattr(baseline_obj, "total_score_17", None)
	hamd_trend_cols = []
	for t, label in timing_order:
		_a = latest_by_timing.get(t)
		_date_str = _a.date.strftime("%Y/%-m/%-d") if _a and getattr(_a, "date", None) else "-"
		ham17 = getattr(_a, "total_score_17", None)
		ham21 = getattr(_a, "total_score_21", None)
		imp = None
		if t != "baseline" and _a and baseline_17 not in (None, 0) and ham17 is not None:
			imp = round((baseline_17 - ham17) / baseline_17 * 100.0, 1)
		sev = _severity_label_17(ham17)
		resp = _response_label(t, ham17, imp)
		hamd_trend_cols.append({
			"timing": t,
			"label": label,
			"date_str": _date_str,
			"hamd17": ham17,
			"hamd21": ham21,
			"improvement_pct": imp,
			"improvement_pct_17": imp,
			"severity_label_17": sev,
			"response_label": resp,
		})

	# HAMD detail grid (per-item scores for 21 items)
	cols = []
	for t, label in timing_order:
		_a = latest_by_timing.get(t)
		cols.append({
			"key": t,
			"label": label,
			"date_str": _a.date.strftime("%Y/%-m/%-d") if _a and getattr(_a, "date", None) else "-",
		})
	# Human-readable Japanese names for HAMD-21 items
	ITEM_NAMES = [
		"抑うつ気分",
		"罪責感",
		"自殺念慮",
		"入眠障害",
		"中途覚醒",
		"早朝覚醒",
		"仕事と活動性（興味・意欲）",
		"精神運動制止",
		"焦燥（精神運動興奮）",
		"不安（精神性）",
		"不安（身体性）",
		"胃腸症状",
		"全身症状",
		"性欲減退",
		"心気症",
		"体重減少",
		"病識",
		"日内変動",
		"離人・現実感消失",
		"妄想（被害・罪業など）",
		"強迫症状",
	]
	rows = []
	for i in range(1, 22):
		# Safe fallback in case of index issues
		name = ITEM_NAMES[i - 1] if 1 <= i <= len(ITEM_NAMES) else f"項目{i}"
		scores_by_t = {}
		for t, _ in timing_order:
			_a = latest_by_timing.get(t)
			val = None
			if _a and isinstance(_a.scores, dict):
				val = _a.scores.get(f"q{i}")
			scores_by_t[t] = val if val is not None else ""
		rows.append({"no": i, "name": name, "scores": scores_by_t})
	_totals = {"hamd17": {}, "hamd21": {}, "improvement_pct": {}}
	baseline17 = getattr(latest_by_timing.get("baseline"), "total_score_17", None)
	for t, _ in timing_order:
		_a = latest_by_timing.get(t)
		t17 = getattr(_a, "total_score_17", None)
		t21 = getattr(_a, "total_score_21", None)
		_totals["hamd17"][t] = t17
		_totals["hamd21"][t] = t21
		if t == "baseline" or (baseline17 in (None, 0) or t17 is None):
			_totals["improvement_pct"][t] = None
		else:
			_totals["improvement_pct"][t] = round((baseline17 - t17) / baseline17 * 100.0, 1)

	context = {
		'patient': patient,
		'questionnaire': questionnaire,
		'assessments': assessments,
		'today': timezone.now().date(),
		'docs_to_render': docs_to_render,
		'doc_templates': DOC_TEMPLATES,
		'back_url': back_url,
		'hamd_insurance_notice': '第３週目の評価において、その合計スコアがＨＡＭＤ１７で７以下、ＨＡＭＤ２４で９以下である場合は寛解と判断し当該治療は中止又は漸減する。漸減する場合、第４週目は最大週３回、第５週目は最大週２回、第６週目は最大週１回まで算定できる。また、第３週目の評価において、ＨＡＭＤ１７又はＨＡＭＤ２４の合計スコアで寛解と判断されず、かつ治療開始前の評価より改善が 20％未満の場合には中止する。',
		'hamd_trend_cols': hamd_trend_cols,
		'hamd_detail': {"cols": cols, "rows": rows, "totals": _totals},
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

	# Build hamd_trend_cols for partial rendering
	timing_order = [("baseline", "治療前"), ("week3", "3週"), ("week4", "4週"), ("week6", "6週")]
	latest_by_timing = {t: Assessment.objects.filter(patient=patient, timing=t).order_by("-date").first() for t, _ in timing_order}
	baseline_obj = latest_by_timing.get("baseline")
	baseline_17 = getattr(baseline_obj, "total_score_17", None)
	def _sev(score):
		if score is None: return None
		return "正常" if score <= 7 else ("軽症" if score <= 13 else ("中等症" if score <= 18 else ("重症" if score <= 22 else "最重症")))
	def _resp(t, s, imp):
		if t == 'baseline': return None
		if s is not None and s <= 7: return "寛解（中止/漸減)" if t == 'week3' else "寛解"
		if imp is not None and imp < 20.0: return "無効（中止）" if t == 'week3' else "無効"
		return "継続"
	hamd_trend_cols = []
	for t, label in timing_order:
		a = latest_by_timing.get(t)
		ds = a.date.strftime("%Y/%-m/%-d") if a and getattr(a, 'date', None) else '-'
		s17 = getattr(a, 'total_score_17', None)
		s21 = getattr(a, 'total_score_21', None)
		imp = None
		if t != 'baseline' and a and baseline_17 not in (None, 0) and s17 is not None:
			imp = round((baseline_17 - s17) / baseline_17 * 100.0, 1)
		hamd_trend_cols.append({
			'timing': t,
			'label': label,
			'date_str': ds,
			'hamd17': s17,
			'hamd21': s21,
			'improvement_pct': imp,
			'improvement_pct_17': imp,
			'severity_label_17': _sev(s17),
			'response_label': _resp(t, s17, imp),
		})
	
	context = {
		'patient': patient,
		'today': timezone.now().date(),
		'test_scores': test_scores,
		'hamd_trend_cols': hamd_trend_cols,
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
	# HAMD trend for referral
	timing_order = [("baseline", "治療前"), ("week3", "3週"), ("week4", "4週"), ("week6", "6週")]
	latest_by_timing = {t: Assessment.objects.filter(patient=patient, timing=t).order_by("-date").first() for t, _ in timing_order}
	baseline_obj = latest_by_timing.get("baseline")
	baseline_17 = getattr(baseline_obj, "total_score_17", None)
	def _sev(score):
		if score is None: return None
		return "正常" if score <= 7 else ("軽症" if score <= 13 else ("中等症" if score <= 18 else ("重症" if score <= 22 else "最重症")))
	def _resp(t, s, imp):
		if t == 'baseline': return None
		if s is not None and s <= 7: return "寛解（中止/漸減)" if t == 'week3' else "寛解"
		if imp is not None and imp < 20.0: return "無効（中止）" if t == 'week3' else "無効"
		return "継続"
	hamd_trend_cols = []
	for t, label in timing_order:
		a = latest_by_timing.get(t)
		ds = a.date.strftime("%Y/%-m/%-d") if a and getattr(a, 'date', None) else '-'
		s17 = getattr(a, 'total_score_17', None)
		s21 = getattr(a, 'total_score_21', None)
		imp = None
		if t != 'baseline' and a and baseline_17 not in (None, 0) and s17 is not None:
			imp = round((baseline_17 - s17) / baseline_17 * 100.0, 1)
		hamd_trend_cols.append({
			'timing': t,
			'label': label,
			'date_str': ds,
			'hamd17': s17,
			'hamd21': s21,
			'improvement_pct': imp,
			'improvement_pct_17': imp,
			'severity_label_17': _sev(s17),
			'response_label': _resp(t, s17, imp),
		})
	context = {
		'patient': patient,
		'today': timezone.now().date(),
		'test_scores': test_scores,
		'hamd_trend_cols': hamd_trend_cols,
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
		# Treatment parameters for display
		'coil_type': getattr(session, 'coil_type', 'BrainsWay H1 coil') or 'BrainsWay H1 coil',
		'frequency_hz': getattr(session, 'frequency_hz', ''),
		'mt_percent': getattr(session, 'mt_percent', ''),
		'total_pulses': getattr(session, 'total_pulses', ''),
		# Computed stimulation time (minutes) for printing
		'stimulation_minutes': getattr(session, 'stimulation_minutes_display', ''),
	}
	return render(request, 'rtms_app/print/side_effect_check.html', context)
