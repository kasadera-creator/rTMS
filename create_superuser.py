import os
import django

# Djangoの設定を読み込む
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth import get_user_model

User = get_user_model()
USERNAME = "admin"
PASSWORD = "adminpassword123"  # ★仮のパスワード（後で変更可）

if not User.objects.filter(username=USERNAME).exists():
    print(f"管理者ユーザー {USERNAME} を作成します...")
    User.objects.create_superuser(USERNAME, 'admin@example.com', PASSWORD)
    print("作成完了！")
else:
    print(f"管理者ユーザー {USERNAME} は既に存在します。")