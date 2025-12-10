from django import forms
from django.contrib.auth.models import User
from .models import Patient, MappingSession, TreatmentSession, Assessment

class DateInput(forms.DateInput):
    input_type = 'date'

class DateTimeInput(forms.DateTimeInput):
    input_type = 'datetime-local'

# --- 1. 新規登録フォーム (簡素化) ---
class PatientRegistrationForm(forms.ModelForm):
    class Meta:
        model = Patient
        # ★修正: 必要最低限の項目のみにする
        fields = ['card_id', 'name', 'birth_date']
        widgets = {
            'card_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '例: 12345'}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '例: 笠寺 太郎'}),
            'birth_date': DateInput(attrs={'class': 'form-control'}),
        }
        labels = {
            'card_id': 'カルテ番号',
            'name': '氏名',
            'birth_date': '生年月日',
        }
    def clean_card_id(self):
        card_id = self.cleaned_data['card_id']
        if Patient.objects.filter(card_id=card_id).exists(): raise forms.ValidationError("登録済み")
        return card_id

# --- 2. 初診フォーム (項目の追加・datalist対応) ---
class PatientFirstVisitForm(forms.ModelForm):
    attending_physician = forms.ModelChoiceField(
        queryset=User.objects.all(), 
        label="担当医", 
        required=False, 
        widget=forms.Select(attrs={'class': 'form-select'})
    )

    class Meta:
        model = Patient
        fields = [
            'card_id', 'name', 'birth_date', 'gender', 'attending_physician',
            'referral_source', 'chief_complaint', # ★主訴を追加
            # diagnosis はテンプレート側でチェックボックスとして扱うため、ここでは除外するかHiddenにする手もありますが、
            # ModelFormとして扱うため含めておき、ウィジェットを調整します
            'diagnosis', 
            'life_history', 'past_history', 'present_illness', 'medication_history',
            'admission_date', 'mapping_date', 'first_treatment_date'
        ]
        widgets = {
            'card_id': forms.TextInput(attrs={'class': 'form-control'}),
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'birth_date': DateInput(attrs={'class': 'form-control'}),
            'gender': forms.Select(attrs={'class': 'form-select'}),
            # 紹介元: datalistと連携するために list属性を追加
            'referral_source': forms.TextInput(attrs={'class': 'form-control', 'list': 'referral-options'}),
            'chief_complaint': forms.TextInput(attrs={'class': 'form-control'}),
            'diagnosis': forms.HiddenInput(), # ★画面上ではチェックボックスで作るため隠す
            'life_history': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'past_history': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'present_illness': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'medication_history': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'admission_date': DateInput(attrs={'class': 'form-control'}),
            'mapping_date': DateInput(attrs={'class': 'form-control'}),
            'first_treatment_date': DateInput(attrs={'class': 'form-control'}),
        }

# --- 以下、変更なし ---
class PatientScheduleForm(forms.ModelForm):
    class Meta:
        model = Patient
        fields = ['admission_date', 'mapping_date', 'first_treatment_date']
        widgets = {'admission_date': DateInput(attrs={'class': 'form-control'}), 'mapping_date': DateInput(attrs={'class': 'form-control'}), 'first_treatment_date': DateInput(attrs={'class': 'form-control'})}

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