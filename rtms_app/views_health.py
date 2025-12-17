"""
ヘルスチェック・システム情報
"""
from django.http import JsonResponse, HttpResponse
from django.db import connection
from django.conf import settings
import os
import sys
from datetime import datetime


def healthz(request):
    """
    ヘルスチェックエンドポイント
    DB接続確認 + 基本情報
    """
    health_status = {
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'checks': {}
    }
    
    # DB接続チェック
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        health_status['checks']['database'] = 'ok'
    except Exception as e:
        health_status['status'] = 'unhealthy'
        health_status['checks']['database'] = f'error: {str(e)}'
        return JsonResponse(health_status, status=500)
    
    # Python version
    health_status['python_version'] = sys.version.split()[0]
    
    # Django settings
    health_status['debug'] = settings.DEBUG
    health_status['environment'] = os.environ.get('DJANGO_ENV', 'unknown')
    
    return JsonResponse(health_status)


def version(request):
    """
    バージョン・ビルド情報
    """
    version_info = {
        'application': 'rTMS Support System',
        'environment': os.environ.get('DJANGO_ENV', 'unknown'),
        'debug': settings.DEBUG,
        'python': sys.version.split()[0],
        'database': settings.DATABASES['default']['ENGINE'].split('.')[-1],
        'static_url': settings.STATIC_URL,
        'timestamp': datetime.now().isoformat(),
    }
    
    # Git commit hash (if available)
    git_sha = os.environ.get('GIT_SHA')
    if git_sha:
        version_info['git_sha'] = git_sha
    
    # Build date (if available)
    build_date = os.environ.get('BUILD_DATE')
    if build_date:
        version_info['build_date'] = build_date
    
    return JsonResponse(version_info)
