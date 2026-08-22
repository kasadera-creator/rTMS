#!/bin/bash
# Production Database Migration Fix Script
# このスクリプトを本番環境で実行してください

echo "=== Django Migration Fix for Production ==="
echo "現在の Python 環境を確認中..."

# Python のバージョン確認
python --version

echo ""
echo "=== Pending Migrations を確認中 ==="
python manage.py showmigrations rtms_app

echo ""
echo "=== マイグレーション実行中... ==="
python manage.py migrate rtms_app

echo ""
echo "=== マイグレーション完了 ==="
python manage.py migrate --list | grep rtms_app

echo ""
echo "✅ 完了。ダッシュボードにアクセスしてエラーが解決したか確認してください。"
