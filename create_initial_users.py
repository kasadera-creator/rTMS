import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth.models import User

def create_users():
    # (username, password, last_name, first_name, is_staff, is_superuser)
    users = [
        ('admin', 'adminpassword123', '管理者', '', True, True),
        ('mori', 'password!', '森', '先生', True, False),
        ('kiya', 'password!', '木谷', '先生', True, False),
        ('furukawa', 'password!', '古川', '先生', True, False),
        ('iwata', 'password!', '岩田', '先生', True, False),
        ('nurse', 'password!', '看護師', 'スタッフ', True, False),
    ]

    for username, password, lname, fname, is_staff, is_superuser in users:
        if not User.objects.filter(username=username).exists():
            print(f"Creating user: {lname} {fname} ({username})")
            User.objects.create_user(
                username=username, 
                password=password, 
                email=f"{username}@example.com",
                last_name=lname,
                first_name=fname,
                is_staff=is_staff, 
                is_superuser=is_superuser
            )
        else:
            # 既にいる場合は名前だけ更新しておく
            u = User.objects.get(username=username)
            u.last_name = lname
            u.first_name = fname
            u.save()
            print(f"Updated user: {lname} {fname}")

if __name__ == "__main__":
    create_users()