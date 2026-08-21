"""
View decorators for common request patterns.

Reduces boilerplate by extracting frequently-used patterns:
- Patient object retrieval
- Dashboard date parameter extraction
- Context building with common fields
"""

from functools import wraps
from django.shortcuts import get_object_or_404
from django.contrib.auth.decorators import login_required
from .models import Patient


def get_patient_and_dashboard(login_required_=True):
	"""
	Decorator to extract patient and dashboard_date from request.
	
	Automatically retrieves:
	- patient: Patient object for given patient_id path parameter
	- dashboard_date: Date parameter from request.GET (if provided)
	
	Usage:
		@get_patient_and_dashboard()
		def my_view(request, patient_id, patient, dashboard_date):
			# patient is automatically retrieved and passed
			# dashboard_date is extracted or None
			...
	
	Args:
		login_required_: If True, apply @login_required decorator
	
	Raises:
		Http404 if patient_id does not exist
	"""
	def decorator(view_func):
		@wraps(view_func)
		def wrapper(request, patient_id, *args, **kwargs):
			# Retrieve patient (404 if not found)
			patient = get_object_or_404(Patient, pk=patient_id)
			
			# Extract dashboard_date from GET parameters
			dashboard_date = request.GET.get('dashboard_date', None)
			
			# Call view with injected patient and dashboard_date
			return view_func(request, patient_id, patient, dashboard_date, *args, **kwargs)
		
		# Apply login_required if specified
		if login_required_:
			wrapper = login_required(wrapper)
		
		return wrapper
	
	return decorator


def extract_dashboard_date(view_func):
	"""
	Simple decorator to extract dashboard_date from GET parameters.
	
	Usage:
		@extract_dashboard_date
		def my_view(request, patient_id, dashboard_date):
			...
	"""
	@wraps(view_func)
	def wrapper(request, *args, **kwargs):
		dashboard_date = request.GET.get('dashboard_date', None)
		return view_func(request, *args, dashboard_date=dashboard_date, **kwargs)
	
	return login_required(wrapper)
