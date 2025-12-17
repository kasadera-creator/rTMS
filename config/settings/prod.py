from .base import *

DEBUG = False

# 本番は * 禁止（現状は ['*']）
allowed_hosts_str = env("DJANGO_ALLOWED_HOSTS", "")
if not allowed_hosts_str or "*" in allowed_hosts_str:
    raise RuntimeError("DJANGO_ALLOWED_HOSTS must be set explicitly (no '*') in production")

# 本番は manifest あり（キャッシュ＆差し替え事故防止）
STORAGES = {
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

CSRF_TRUSTED_ORIGINS = [o.strip() for o in env("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()]
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True

