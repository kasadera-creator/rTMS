from django.contrib import admin
from .models import Patient, TreatmentSession

admin.site.register(Patient)
admin.site.register(TreatmentSession)