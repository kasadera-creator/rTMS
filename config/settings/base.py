from pathlib import Path
import os
import dj_database_url

# Note: .env is already loaded in config/settings.py before this module is imported
BASE_DIR = Path(__file__).resolve().parent.parent.parent  # ~/rTMS

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

def env(key: str, default=None):
    return os.environ.get(key, default)

def env_bool(key: str, default="0"):
    return str(env(key, default)).lower() in ("1", "true", "yes", "on")

# --- Core ---
SECRET_KEY = env("DJANGO_SECRET_KEY", env("SECRET_KEY", "dev-insecure-secret-key"))
DEBUG = env_bool("DJANGO_DEBUG", "0")

ALLOWED_HOSTS = os.environ.get(
    "DJANGO_ALLOWED_HOSTS",
    "localhost,127.0.0.1,rtms.local"
).split(",")

INSTALLED_APPS = [
    "jazzmin",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rtms_app",
    "django_jsonform",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "rtms_app.middleware.RequestMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# --- DB ---
DATABASE_URL = env("DATABASE_URL")
if DATABASE_URL:
    DATABASES = {
        "default": dj_database_url.config(
            default=DATABASE_URL,
            conn_max_age=600,
            ssl_require=True,
        )
    }
else:
    DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": BASE_DIR / "db.sqlite3"}}

# --- i18n ---
LANGUAGE_CODE = "ja"
TIME_ZONE = "Asia/Tokyo"
USE_I18N = True
USE_TZ = True

# --- Static ---
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# dev/prodで切替するため、ここではmanifest無しの安全側
STORAGES = {
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

LOGIN_URL = "/admin/login/"
LOGIN_REDIRECT_URL = "/app/dashboard/"
LOGOUT_REDIRECT_URL = "/admin/login/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Logging（500根治のため、django.request は ERROR以上を必ず出す）
# NOTE:
# - DJANGO_LOG_LEVEL controls application/framework loggers.
# - DJANGO_CONSOLE_LOG_LEVEL controls what actually prints to terminal.
#   This prevents accidental DEBUG spam (SQL/autoreload) even if DJANGO_LOG_LEVEL=DEBUG.
LOG_LEVEL = env("DJANGO_LOG_LEVEL", "INFO")
CONSOLE_LOG_LEVEL = env("DJANGO_CONSOLE_LOG_LEVEL", "INFO")
DB_LOG_LEVEL = env("DJANGO_DB_LOG_LEVEL", "WARNING")
AUTORELOAD_LOG_LEVEL = env("DJANGO_AUTORELOAD_LOG_LEVEL", "WARNING")
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler", "level": CONSOLE_LOG_LEVEL}},
    "loggers": {
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": True},
        "django": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
        # Keep terminal output readable in dev by default.
        # Enable explicitly by setting DJANGO_DB_LOG_LEVEL=DEBUG etc.
        "django.db.backends": {"handlers": ["console"], "level": DB_LOG_LEVEL, "propagate": False},
        "django.utils.autoreload": {"handlers": ["console"], "level": AUTORELOAD_LOG_LEVEL, "propagate": False},
        "watchfiles": {"handlers": ["console"], "level": AUTORELOAD_LOG_LEVEL, "propagate": False},
        "whitenoise": {"handlers": ["console"], "level": LOG_LEVEL, "propagate": False},
    },
}

# Jazzmin（現行踏襲）
JAZZMIN_SETTINGS = {
    "site_title": "笠寺精治寮病院 rTMS支援システム",
    "site_header": "笠寺精治寮病院 rTMS支援システム",
    "site_brand": "笠寺精治寮病院 rTMS支援システム",
    "welcome_sign": "笠寺精治寮病院 rTMS支援システム",
    "copyright": "K. Iwata @ Kasadera Seichiryo Hospital",
    "site_logo": "img/logo.jpg",
    "navigation_expanded": True,
    "topmenu_links": [{"name": "◀ トップへ戻る", "url": "/app/dashboard/"}],
    "order_with_respect_to": [
        "rtms_app.ConsentDocument",
        "rtms_app.Patient",
        "rtms_app.TreatmentSession",
        "rtms_app.Assessment",
    ],
    "icons": {
        "rtms_app.Patient": "fas fa-user-injured",
        "rtms_app.TreatmentSession": "fas fa-procedures",
        "rtms_app.Assessment": "fas fa-chart-line",
    },
    "custom_css": "css/admin_custom.css",
}

JAZZMIN_UI_TWEAKS = {"theme": "flatly", "dark_mode_theme": None}
