from django import forms
from django.contrib.auth.models import User
from .models import Patient, MappingSession, TreatmentSession, Assessment

class DateInput(forms.DateInput):
    input_type = 'date'

class DateTimeInput(forms.DateTimeInput):
    input_type = 'datetime-local'

class PhysicianChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        name = f"{obj.last_name} {obj.first_name}" if obj.last_name else obj.username
        return f"{name}"

# --- 1. 新規登録フォーム ---
class PatientRegistrationForm(forms.ModelForm):
    class Meta:
        model = Patient
        fields = ['card_id', 'name', 'birth_date', 'gender', 'referral_source', 'referral_doctor']
        widgets = {
            'card_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '例: 12345'}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '例: 笠寺 太郎'}),
            'birth_date': DateInput(attrs={'class': 'form-control'}),
            'gender': forms.Select(attrs={'class': 'form-select'}),
            'referral_source': forms.TextInput(attrs={'class': 'form-control', 'list': 'referral-options', 'placeholder': '医療機関名 (任意)'}),
            'referral_doctor': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '医師名 (任意)'}),
        }
    
    # ★変更: clean_card_id を削除し、重複チェックはView側で行う

# --- 2. 初診フォーム ---
class PatientFirstVisitForm(forms.ModelForm):
    attending_physician = PhysicianChoiceField(
        queryset=User.objects.filter(groups__name='医師'),
        label="担当医", required=False, widget=forms.Select(attrs={'class': 'form-select'})
    )
    
    diagnosis = forms.CharField(widget=forms.HiddenInput(), required=False)

    class Meta:
        model = Patient
        fields = [
            'card_id', 'name', 'birth_date', 'gender', 'attending_physician', 
            'referral_source', 'referral_doctor',
            'chief_complaint', 'diagnosis', 
            'life_history', 'past_history', 'present_illness', 'medication_history', 
            'admission_date', 'mapping_date', 'first_treatment_date'
        ]
        widgets = {
            'card_id': forms.TextInput(attrs={'class': 'form-control'}),
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'birth_date': DateInput(attrs={'class': 'form-control'}),
            'gender': forms.Select(attrs={'class': 'form-select'}),
            'referral_source': forms.TextInput(attrs={'class': 'form-control', 'list': 'referral-options', 'placeholder': '医療機関名'}),
            'referral_doctor': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '医師名 (姓のみ、またはフルネーム)'}),
            'chief_complaint': forms.TextInput(attrs={'class': 'form-control'}),
            'life_history': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'past_history': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'present_illness': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'medication_history': forms.Textarea(attrs={'class': 'form-control', 'rows': 6}),
            'dementia_detail': forms.TextInput(attrs={
                'class': 'form-control form-control-sm',
                'placeholder': '',
                'style': 'display: inline-block; width: auto; margin-left: 10px;'
            }),
            'admission_date': DateInput(attrs={'class': 'form-control'}),
            'mapping_date': DateInput(attrs={'class': 'form-control'}),
            'first_treatment_date': DateInput(attrs={'class': 'form-control'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        default_medication_text = (
            "抗うつ薬：\n"
            "抗精神病薬：\n"
            "気分安定薬：\n"
            "抗不安薬：\n"
            "睡眠薬：\n"
            "その他："
        )
        if not self.instance.medication_history:
            self.instance.medication_history = default_medication_text
            self.initial['medication_history'] = default_medication_text

class MappingForm(forms.ModelForm):
    class Meta:
        model = MappingSession
        fields = ['date', 'week_number', 'resting_mt', 'stimulation_site', 'notes']
        widgets = {'date': DateInput(attrs={'class': 'form-control'}), 'week_number': forms.Select(attrs={'class': 'form-select'}), 'resting_mt': forms.NumberInput(attrs={'class': 'form-control'}), 'stimulation_site': forms.TextInput(attrs={'class': 'form-control'}), 'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2})}

class TreatmentForm(forms.ModelForm):
    class Meta:
        model = TreatmentSession
        fields = ['date', 'safety_sleep', 'safety_alcohol', 'safety_meds', 'motor_threshold', 'intensity', 'total_pulses']
        widgets = {'date': DateTimeInput(attrs={'class': 'form-control'}), 'motor_threshold': forms.NumberInput(attrs={'class': 'form-control'}), 'intensity': forms.NumberInput(attrs={'class': 'form-control'}), 'total_pulses': forms.NumberInput(attrs={'class': 'form-control'})}

class AdmissionProcedureForm(forms.ModelForm):
    class Meta:
        model = Patient
        fields = ['admission_type', 'is_admission_procedure_done']
        widgets = {'admission_type': forms.RadioSelect(attrs={'class': 'form-check-input'})}