from django import forms
from django.contrib.auth.models import User
from .models import Patient, MappingSession, TreatmentSession, Assessment

# ------------------------------------------------------------------
# 共通ウィジェット設定
# ------------------------------------------------------------------
class DateInput(forms.DateInput):
    input_type = 'date'

class DateTimeInput(forms.DateTimeInput):
    input_type = 'datetime-local'

# ------------------------------------------------------------------
# 1. 患者新規登録フォーム (トップ画面の「新規登録」用)
# ------------------------------------------------------------------
class PatientRegistrationForm(forms.ModelForm):
    class Meta:
        model = Patient
        fields = [
            'card_id', 'name', 'birth_date', 'diagnosis', 
            'admission_date', 'mapping_date', 'first_treatment_date'
        ]
        widgets = {
            'card_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '例: 12345'}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '例: 笠寺 太郎'}),
            'birth_date': DateInput(attrs={'class': 'form-control'}),
            'diagnosis': forms.TextInput(attrs={'class': 'form-control', 'value': 'うつ病'}),
            'admission_date': DateInput(attrs={'class': 'form-control'}),
            'mapping_date': DateInput(attrs={'class': 'form-control'}),
            'first_treatment_date': DateInput(attrs={'class': 'form-control'}),
        }
        labels = {
            'card_id': 'カルテ番号',
            'name': '氏名',
            'birth_date': '生年月日',
            'diagnosis': '診断名',
            'admission_date': '入院予定日',
            'mapping_date': '初回位置決め日',
            'first_treatment_date': '初回治療日',
        }
    
    def clean_card_id(self):
        card_id = self.cleaned_data['card_id']
        if Patient.objects.filter(card_id=card_id).exists():
            raise forms.ValidationError("このカルテ番号は既に登録されています。")
        return card_id

# ------------------------------------------------------------------
# 2. 初診・基本情報入力フォーム (詳細画面用)
# ------------------------------------------------------------------
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
            'referral_source', 'diagnosis',
            'life_history', 'past_history', 'present_illness', 'medication_history',
            # questionnaire_data はView/Template側で制御、または必要に応じて追加
        ]
        widgets = {
            'card_id': forms.TextInput(attrs={'class': 'form-control'}),
            'name': forms.TextInput(attrs={'class': 'form-control'}),
            'birth_date': DateInput(attrs={'class': 'form-control'}),
            'gender': forms.Select(attrs={'class': 'form-select'}),
            'referral_source': forms.TextInput(attrs={'class': 'form-control'}),
            'diagnosis': forms.TextInput(attrs={'class': 'form-control'}),
            'life_history': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'past_history': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'present_illness': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
            'medication_history': forms.Textarea(attrs={'class': 'form-control', 'rows': 3}),
        }
        labels = {
            'card_id': 'カルテ番号',
            'name': '氏名',
            'birth_date': '生年月日',
            'gender': '性別',
            'referral_source': '紹介元',
            'diagnosis': '診断名',
        }

# ------------------------------------------------------------------
# 3. スケジュール管理フォーム (エラーの原因だったもの)
# ------------------------------------------------------------------
class PatientScheduleForm(forms.ModelForm):
    class Meta:
        model = Patient
        fields = ['admission_date', 'mapping_date', 'first_treatment_date']
        widgets = {
            'admission_date': DateInput(attrs={'class': 'form-control'}),
            'mapping_date': DateInput(attrs={'class': 'form-control'}),
            'first_treatment_date': DateInput(attrs={'class': 'form-control'}),
        }
        labels = {
            'admission_date': '入院予定日',
            'mapping_date': '初回位置決め日',
            'first_treatment_date': '初回治療日',
        }

# ------------------------------------------------------------------
# 4. 位置決めフォーム
# ------------------------------------------------------------------
class MappingForm(forms.ModelForm):
    class Meta:
        model = MappingSession
        fields = ['date', 'week_number', 'resting_mt', 'stimulation_site', 'notes']
        widgets = {
            'date': DateInput(attrs={'class': 'form-control'}),
            'week_number': forms.Select(attrs={'class': 'form-select'}),
            'resting_mt': forms.NumberInput(attrs={'class': 'form-control'}),
            'stimulation_site': forms.TextInput(attrs={'class': 'form-control'}),
            'notes': forms.Textarea(attrs={'class': 'form-control', 'rows': 2}),
        }
        labels = {
            'date': '実施日',
            'week_number': '時期',
            'resting_mt': '安静時MT (%)',
            'stimulation_site': '刺激部位',
            'notes': '特記事項',
        }

# ------------------------------------------------------------------
# 5. 治療実施フォーム
# ------------------------------------------------------------------
class TreatmentForm(forms.ModelForm):
    class Meta:
        model = TreatmentSession
        fields = ['date', 'safety_sleep', 'safety_alcohol', 'safety_meds', 
                  'motor_threshold', 'intensity', 'total_pulses']
        widgets = {
            'date': DateTimeInput(attrs={'class': 'form-control'}),
            'motor_threshold': forms.NumberInput(attrs={'class': 'form-control'}),
            'intensity': forms.NumberInput(attrs={'class': 'form-control'}),
            'total_pulses': forms.NumberInput(attrs={'class': 'form-control'}),
        }
        labels = {
            'date': '実施日時',
            'safety_sleep': '睡眠不足なし',
            'safety_alcohol': 'アルコール・カフェイン過剰なし',
            'safety_meds': '服薬変更なし',
            'motor_threshold': '当日のMT (%)',
            'intensity': '刺激強度 (%)',
            'total_pulses': '総パルス数',
        }