from django import forms
from django.contrib.auth.models import User
from .models import Patient, MappingSession, TreatmentSession, Assessment

class DateInput(forms.DateInput):
    input_type = 'date'

class DateTimeInput(forms.DateTimeInput):
    input_type = 'datetime-local'

# 担当医の表示名を日本語にするためのカスタムフィールド
class PhysicianChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, obj):
        name = f"{obj.last_name} {obj.first_name}" if obj.last_name else obj.username
        return f"{name}"

# --- 1. 新規登録フォーム ---
class PatientRegistrationForm(forms.ModelForm):
    class Meta:
        model = Patient
        fields = ['card_id', 'name', 'birth_date']
        widgets = {
            'card_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '例: 12345'}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '例: 笠寺 太郎'}),
            'birth_date': DateInput(attrs={'class': 'form-control'}),
        }
    def clean_card_id(self):
        card_id = self.cleaned_data['card_id']
        if Patient.objects.filter(card_id=card_id).exists(): raise forms.ValidationError("登録済み")
        return card_id

# --- 2. 初診フォーム ---
class PatientFirstVisitForm(forms.ModelForm):
    attending_physician = PhysicianChoiceField(
        queryset=User.objects.filter(groups__name='医師'),
        label="担当医", required=False, widget=forms.Select(attrs={'class': 'form-select'})
    )
    
    class Meta:
        model = Patient
        fields = [
            'card_id', 'name', 'birth_date', 'gender', 'attending_physician', 
            'referral_source', 'chief_complaint', 'diagnosis', 
            'life_history', 'past_history', 'present_illness', 'medication_history', 
            'admission_date', 'mapping_date', 'first_treatment_date'
        ]
        widgets = {
            'card_id': forms.TextInput(attrs={'class': 'form-control'}),
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'birth_date': DateInput(attrs={'class': 'form-control'}),
            'gender': forms.Select(attrs={'class': 'form-select'}),
            'referral_source': forms.TextInput(attrs={'class': 'form-control', 'list': 'referral-options'}),
            'chief_complaint': forms.TextInput(attrs={'class': 'form-control'}),
            'diagnosis': forms.HiddenInput(),
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

    # ★追加: 初期値設定
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # 薬剤治療歴が空の場合、テンプレートをセット
        if not self.instance.medication_history:
            self.fields['medication_history'].initial = (
                "抗うつ薬：\n"
                "抗精神病薬：\n"
                "気分安定薬：\n"
                "抗不安薬：\n"
                "睡眠薬：\n"
                "その他："
            )

# --- 3. 位置決めフォーム ---
class MappingForm(forms.ModelForm):
    class Meta:
        model = MappingSession
        fields = ['date', 'week_number', 'resting_mt', 'stimulation_site', 'notes']
        widgets = {'date': DateInput(attrs={'class': 'form-control'}), 'week_number': forms.Select(attrs={'class': 'form-select'}), 'resting_mt': forms.NumberInput(attrs={'class': 'form-control'}), 'stimulation_site': forms.TextInput(attrs={'class': 'form-control'}), 'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2})}

# --- 4. 治療実施フォーム ---
class TreatmentForm(forms.ModelForm):
    class Meta:
        model = TreatmentSession
        fields = ['date', 'safety_sleep', 'safety_alcohol', 'safety_meds', 'motor_threshold', 'intensity', 'total_pulses']
        widgets = {'date': DateTimeInput(attrs={'class': 'form-control'}), 'motor_threshold': forms.NumberInput(attrs={'class': 'form-control'}), 'intensity': forms.NumberInput(attrs={'class': 'form-control'}), 'total_pulses': forms.NumberInput(attrs={'class': 'form-control'})}

# --- 5. 入院手続きフォーム ---
class AdmissionProcedureForm(forms.ModelForm):
    class Meta:
        model = Patient
        fields = ['admission_type', 'is_admission_procedure_done']
        widgets = {'admission_type': forms.RadioSelect(attrs={'class': 'form-check-input'})}