import os
from pathlib import Path
import sys
from django.core.exceptions import ImproperlyConfigured

# Load .env file IMMEDIATELY before any condition checks
# This ensures DJANGO_ENV and DJANGO_DEBUG are available
try:
    from dotenv import load_dotenv
    BASE_DIR = Path(__file__).resolve().parent.parent.parent  # ~/rTMS
    load_dotenv(BASE_DIR / ".env", override=False)
except ImportError:
    pass

_django_env = os.environ.get("DJANGO_ENV", "")
_render = os.environ.get("RENDER", "")

def is_production_environment(django_env=None, render=None):
    normalized_env = (django_env if django_env is not None else "").strip()
    if render:
        return True
    if normalized_env in ("", "dev"):
        return False
    if normalized_env == "prod":
        return True
    raise ImproperlyConfigured(
        "DJANGO_ENV must be empty, 'dev', or 'prod'."
    )

_loading_prod = is_production_environment(_django_env, _render)

# 既存の DJANGO_SETTINGS_MODULE=config.settings を壊さないための互換レイヤ
# Render等の本番は prod、それ以外は dev を既定にする
if _loading_prod:
    from .prod import *  # noqa
else:
    from .dev import *  # noqa
