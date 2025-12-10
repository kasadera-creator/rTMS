#!/usr/bin/env bash
# exit on error
set -o errexit

pip install -r requirements.txt

# 静的ファイルの収集
python manage.py collectstatic --no-input

# データベースの構築（マイグレーション）
python manage.py migrate

# ★追加: 管理者ユーザー、初期ユーザー作成スクリプトを実行
python create_superuser.py
python create_initial_users.py