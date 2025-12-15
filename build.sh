#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt

python manage.py collectstatic --no-input
python manage.py migrate

# 初期ユーザー作成は “必要なときだけ”
if [ "${RUN_BOOTSTRAP:-0}" = "1" ]; then
  python create_superuser.py
  python create_initial_users.py
fi
