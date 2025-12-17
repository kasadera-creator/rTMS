"""
カスタムミドルウェア
"""
import uuid
import logging
from .utils.request_context import _thread_locals

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