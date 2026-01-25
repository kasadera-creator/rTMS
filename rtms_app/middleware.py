"""
カスタムミドルウェア
"""
import uuid
import logging

from django.conf import settings
from django.http import HttpResponseForbidden
from django.shortcuts import redirect

from .utils.request_context import _thread_locals
from .services.patient_accounts import PATIENT_GROUP_NAME

logger = logging.getLogger(__name__)


class RequestMiddleware:
    """
    リクエスト毎に一意なIDを付与してログ追跡を容易にする
    既存のthread local設定も維持
    """
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # 既存のthread local設定
        _thread_locals.request = request
        
        # リクエストIDを生成・付与
        request.request_id = str(uuid.uuid4())[:8]
        request.META['HTTP_X_REQUEST_ID'] = request.request_id
        
        # ログに記録
        logger.debug(
            f"[{request.request_id}] {request.method} {request.path} "
            f"user={getattr(request.user, 'username', 'anonymous')}"
        )
        
        try:
            response = self.get_response(request)
            # レスポンスヘッダーにもIDを含める
            response['X-Request-ID'] = request.request_id
            return response
        except Exception as e:
            # 例外発生時に詳細ログ
            logger.error(
                f"[{request.request_id}] Exception in {request.path}: "
                f"{e.__class__.__name__}: {str(e)}",
                extra={
                    'request_id': request.request_id,
                    'user': getattr(request.user, 'username', 'anonymous'),
                    'path': request.path,
                    'method': request.method,
                    'query_string': request.META.get('QUERY_STRING', ''),
                },
                exc_info=True
            )
            raise


class PatientAccessMiddleware:
    """Restrict patient-group users to the patient portal only."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = getattr(request, "user", None)
        if not user or not user.is_authenticated:
            return self.get_response(request)
        if not user.groups.filter(name=PATIENT_GROUP_NAME).exists():
            return self.get_response(request)

        allowed_prefixes = ("/patient/", settings.STATIC_URL, settings.MEDIA_URL)
        if any(request.path.startswith(prefix) for prefix in allowed_prefixes):
            return self.get_response(request)

        if request.method == "GET":
            return redirect("/patient/")
        return HttpResponseForbidden("患者用アカウントではアクセスできません。")