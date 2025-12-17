from django import forms
from django.contrib.auth.models import User
from .models import Patient, MappingSession, TreatmentSession, Assessment
import datetime

class DateInput(forms.DateInput):
    input_type = 'date'

class TimeInput(forms.TimeInput):
    input_type = 'time'

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

# --- 2. 初診フォーム (修正: バリデーション追加) ---
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
            'dementia_detail': forms.TextInput(attrs={'class': 'form-control form-control-sm', 'placeholder': '', 'style': 'display: inline-block; width: auto; margin-left: 10px;'}),
            'admission_date': DateInput(attrs={'class': 'form-control'}),
            'mapping_date': DateInput(attrs={'class': 'form-control'}),
            'first_treatment_date': DateInput(attrs={'class': 'form-control'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        default_medication_text = ("抗うつ薬：\n抗精神病薬：\n気分安定薬：\n抗不安薬：\n睡眠薬：\nその他：")
        if not self.instance.medication_history:
            self.instance.medication_history = default_medication_text
            self.initial['medication_history'] = default_medication_text
    
    # ★追加: 保存時のバリデーション
    def clean(self):
        cleaned_data = super().clean()
        
        # 必須チェックを行う項目
        required_fields = {
            'admission_date': '入院予定日',
            'mapping_date': '初回位置決め日',
            'first_treatment_date': '初回治療日',
            'attending_physician': '今後の担当医',
        }
        
        errors = []
        for field_name, label in required_fields.items():
            if not cleaned_data.get(field_name):
                self.add_error(field_name, f"{label}を入力してください。")
        
        return cleaned_data

# --- 3. 位置決めフォーム ---
class MappingForm(forms.ModelForm):
    class Meta:
        model = MappingSession
        fields = [
            'date', 'week_number',
            'stimulus_intensity_mt_percent', 'intensity_percent',
            'helmet_position_a_x', 'helmet_position_a_y',
            'helmet_position_b_x', 'helmet_position_b_y',
            'notes'
        ]
        widgets = {
            'date': DateInput(attrs={'class': 'form-control'}),
            'week_number': forms.Select(attrs={'class': 'form-select'}),
            'stimulus_intensity_mt_percent': forms.NumberInput(attrs={'class': 'form-control', 'value': '120'}),
            'intensity_percent': forms.NumberInput(attrs={'class': 'form-control', 'value': '60'}),
            'helmet_position_a_x': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'placeholder': 'X座標'}),
            'helmet_position_a_y': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'placeholder': 'Y座標'}),
            'helmet_position_b_x': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'placeholder': 'X座標'}),
            'helmet_position_b_y': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.01', 'placeholder': 'Y座標'}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2})
        }

# --- 4. 治療実施フォーム ---
class TreatmentForm(forms.ModelForm):
    # ★修正: 日付と時間を分離
    treatment_date = forms.DateField(label='実施日', widget=DateInput(attrs={'class': 'form-control', 'required': True}))
    treatment_time = forms.TimeField(label='開始時間', widget=TimeInput(attrs={'class': 'form-control', 'required': True}))

    class Meta:
        model = TreatmentSession
        fields = [
            'safety_sleep', 'safety_alcohol', 'safety_meds',
            'coil_type', 'target_site',
            'mt_percent', 'intensity_percent',
            'frequency_hz', 'train_seconds', 'intertrain_seconds',
            'train_count', 'total_pulses',
            'treatment_notes',
        ]
        widgets = {
            'coil_type': forms.TextInput(attrs={'class': 'form-control', 'required': True, 'value': 'BrainsWay H1'}),
            'target_site': forms.TextInput(attrs={'class': 'form-control', 'required': True, 'value': '左DLPFC'}),
            'mt_percent': forms.NumberInput(attrs={'class': 'form-control', 'required': True, 'min': 0}),
            'intensity_percent': forms.NumberInput(attrs={'class': 'form-control', 'required': True, 'min': 0}),
            'frequency_hz': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.1', 'required': True, 'value': 18}),
            'train_seconds': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.1', 'required': True, 'value': 2}),
            'intertrain_seconds': forms.NumberInput(attrs={'class': 'form-control', 'step': '0.1', 'required': True, 'value': 20}),
            'train_count': forms.NumberInput(attrs={'class': 'form-control', 'required': True, 'min': 1, 'value': 55}),
            'total_pulses': forms.NumberInput(attrs={'class': 'form-control', 'required': True, 'min': 0, 'value': 1980}),
            'treatment_notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }

# --- 5. 入院手続きフォーム ---
class AdmissionProcedureForm(forms.ModelForm):
    class Meta:
        model = Patient
        fields = ['admission_type', 'is_admission_procedure_done']
        widgets = {'admission_type': forms.RadioSelect(attrs={'class': 'form-check-input'})}