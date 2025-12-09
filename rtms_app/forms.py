from django import forms
from .models import Patient
import datetime

class PatientRegistrationForm(forms.ModelForm):
    """現場用の簡易患者登録フォーム"""
    class Meta:
        model = Patient
        fields = ['card_id', 'name', 'birth_date', 'diagnosis', 'medical_history']
        widgets = {
            'card_id': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '例: 12345'}),
            'name': forms.TextInput(attrs={'class': 'form-control', 'placeholder': '例: 笠寺 太郎'}),
            'birth_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}), # カレンダー入力
            'diagnosis': forms.TextInput(attrs={'class': 'form-control', 'value': 'うつ病'}),
            # JSONFieldは現場では複雑なので、一旦隠すか簡易化しますが、
            # ここではdjango-jsonformを使わない標準フォームとして定義します
        }
        labels = {
            'card_id': 'カルテ番号 (ID)',
            'name': '患者氏名',
            'birth_date': '生年月日',
            'diagnosis': '診断名',
        }

    # バリデーション（入力チェック）の追加
    def clean_card_id(self):
        card_id = self.cleaned_data['card_id']
        if Patient.objects.filter(card_id=card_id).exists():
            raise forms.ValidationError("このカルテ番号は既に登録されています。")
        return card_id