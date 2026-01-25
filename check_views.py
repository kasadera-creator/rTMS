#!/usr/bin/env python
import os, sys, django
import traceback

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')
django.setup()

try:
    import rtms_app.views as views_module
    print(f"Views module loaded: {views_module.__file__}")
    print(f"'export_patient_surveys_csv' in dir(views): {('export_patient_surveys_csv' in dir(views_module))}")
    
    # Get all functions starting with export_
    exports = [name for name in dir(views_module) if name.startswith('export_')]
    print(f'Export functions found: {exports}')
    
    # Try to access the function directly
    try:
        func = getattr(views_module, 'export_patient_surveys_csv')
        print(f"Function found: {func}")
    except AttributeError as e:
        print(f"AttributeError: {e}")
        
    # Check if models import works
    from rtms_app.models import PatientSurveySession
    print(f"PatientSurveySession model: {PatientSurveySession}")
    
except Exception as e:
    print(f"Error importing views: {e}")
    traceback.print_exc()
