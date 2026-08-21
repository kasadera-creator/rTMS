"""
View helpers for common patterns in rtms_app views.

Reduces boilerplate by providing reusable functions for:
- Back URL extraction
- Dashboard date extraction
- Context building with common fields
"""

from django.shortcuts import reverse
from django.utils import timezone


def extract_back_url(request, default_view_name, *args, **kwargs):
	"""
	Extract back_url from request with fallback chain.
	
	Priority:
	1. request.GET.get('back_url')
	2. request.META.get('HTTP_REFERER')
	3. reverse(default_view_name, args=args, kwargs=kwargs)
	
	Args:
		request: HTTP request
		default_view_name: Django view name for fallback
		*args, **kwargs: Arguments for reverse()
	
	Returns:
		URL string
	"""
	back_url = request.GET.get('back_url')
	if not back_url:
		back_url = request.META.get('HTTP_REFERER')
	if not back_url:
		back_url = reverse(default_view_name, args=args, kwargs=kwargs)
	return back_url


def get_dashboard_date(request):
	"""
	Extract dashboard_date from request GET parameters.
	
	Returns:
		date string (YYYY-MM-DD) if present, else None
	"""
	return request.GET.get('dashboard_date', None)


def build_common_context(patient, dashboard_date=None, **extra):
	"""
	Build common context dict for views.
	
	Includes:
	- patient
	- dashboard_date (if provided)
	- today
	- can_view_audit (boolean based on user permissions)
	- Any additional key-value pairs passed as **extra
	
	Args:
		patient: Patient model instance
		dashboard_date: Optional date string
		**extra: Additional context items
	
	Returns:
		Dictionary with common and extra context
	"""
	from .models import Patient
	
	context = {
		'patient': patient,
		'today': timezone.now().date(),
	}
	
	if dashboard_date:
		context['dashboard_date'] = dashboard_date
	
	# Merge extra context
	context.update(extra)
	
	return context


def get_course_number(patient):
	"""
	Safely extract course_number from patient.
	
	Returns:
		course_number if set, else 1 (default)
	"""
	return getattr(patient, 'course_number', None) or 1
