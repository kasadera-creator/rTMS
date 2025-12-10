import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth.models import User, Group

def create_users():
    # ユーザーリスト (username, password, is_staff, is_superuser)
    users = [
        ('admin', 'adminpassword123', True, True),
        ('mori', 'password!', True, False),     # 医師
        ('kiya', 'password!', True, False),     # 医師
        ('furukawa', 'password!', True, False), # 医師
        ('iwata', 'password!', True, False),    # 医師
        ('nurse', 'password!', True, False),    # 看護師
    ]

    for username, password, is_staff, is_superuser in users:
        if not User.objects.filter(username=username).exists():
            print(f"Creating user: {username}")
            User.objects.create_user(
                username=username, 
                password=password, 
                email=f"{username}@example.com",
                is_staff=is_staff, 
                is_superuser=is_superuser
            )
        else:
            print(f"User {username} already exists.")

if __name__ == "__main__":
    create_users()