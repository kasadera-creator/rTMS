import os
import django
from datetime import date, datetime
from django.utils import timezone

# Djangoの設定を読み込む
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth.models import User, Group
from rtms_app.models import Patient

def create_initial_data():
    # ---------------------------------------------------------
    # 1. グループ（職種）の作成
    # ---------------------------------------------------------
    group_names = ['管理者', '医師', '看護師', '作業療法士', '心理士', '薬剤師', '事務']
    print("--- Creating Groups ---")
    for name in group_names:
        Group.objects.get_or_create(name=name)

    # ---------------------------------------------------------
    # 2. ユーザーの作成 (姓名を分離・先生なし)
    # ---------------------------------------------------------
    # フォーマット: (username, password, last_name, first_name, is_staff, is_superuser, group_name)
    # ※実在する先生のお名前に書き換えてください。ここでは仮の名を使用しています。
    users_data = [
        ('admin', 'adminpassword123', '管理者', '太郎', True, True, '管理者'),
        ('mori', 'password!', '森', '◯◯', True, False, '医師'),      # 仮名
        ('kiya', 'password!', '木谷', '◯◯', True, False, '医師'),    # 仮名
        ('furukawa', 'password!', '古川', '◯◯', True, False, '医師'), # 仮名
        ('iwata', 'password!', '岩田', '◯◯', True, False, '医師'),    # 仮名
        ('nurse', 'password!', '看護', '◯◯', True, False, '看護師'),
    ]

    print("\n--- Creating Users ---")
    for username, password, lname, fname, is_staff, is_superuser, group_name in users_data:
        # 既存ユーザーがいれば情報を更新、なければ作成
        user, created = User.objects.get_or_create(username=username)
        user.set_password(password)
        user.email = f"{username}@example.com"
        user.last_name = lname
        user.first_name = fname
        user.is_staff = is_staff
        user.is_superuser = is_superuser
        user.save()

        if group_name:
            group = Group.objects.get(name=group_name)
            user.groups.add(group)
        
        print(f"User: {lname} {fname} ({username}) - Updated/Created")

    # ---------------------------------------------------------
    # 3. デフォルト患者の作成
    # ---------------------------------------------------------
    print("\n--- Creating Dummy Patient ---")
    
    # 担当医 'iwata' を取得
    try:
        doctor_iwata = User.objects.get(username='iwata')
    except User.DoesNotExist:
        doctor_iwata = None

    # 患者データ
    dummy_data = {
        'card_id': '12345',
        'name': 'だみーだよ',
        'birth_date': date(1999, 9, 9),
        'gender': 'M',
        'referral_source': '精治寮病院（荒畑）',
        'chief_complaint': 'うつが治らない',
        'life_history': '同胞１名中第１子として名古屋市にて出生。発育・発達に特記事項なし。大学を卒業後、現在の会社に就職。未婚、独居。',
        'past_history': '特になし',
        'present_illness': '2025年4月、会社での部署異動に伴い人間関係に悩むようになり、6月から休職。抗うつ薬および増強療法による薬物療法を中心に治療を継続してきたが、改善せずrTMS目的で紹介となった。2025年12月10日初診。',
        'medication_history': 'エスシタロプラム20mg 10週間、アリピプラゾール3mg 4週間',
        'diagnosis': 'うつ病',
        'admission_date': date(2025, 12, 10),
        'attending_physician': doctor_iwata,
    }

    patient, created = Patient.objects.get_or_create(
        card_id=dummy_data['card_id'],
        defaults=dummy_data
    )

    if created:
        # 初診日(created_at)を指定日に強制変更
        target_date = datetime(2025, 12, 10, 10, 0, 0, tzinfo=timezone.get_current_timezone())
        Patient.objects.filter(pk=patient.pk).update(created_at=target_date)
        print(f"Created patient: {patient.name}")
    else:
        # 既存患者の担当医を更新（ユーザー再作成でIDが変わる可能性があるため）
        patient.attending_physician = doctor_iwata
        patient.save()
        print(f"Patient updated: {patient.name}")

if __name__ == "__main__":
    create_initial_data()