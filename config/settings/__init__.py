import os
from pathlib import Path
import sys

# Load .env file IMMEDIATELY before any condition checks
# This ensures DJANGO_ENV and DJANGO_DEBUG are available
try:
    from dotenv import load_dotenv
    BASE_DIR = Path(__file__).resolve().parent.parent.parent  # ~/rTMS
    load_dotenv(BASE_DIR / ".env", override=False)
except ImportError:
    pass

# Debug: Print what we're loading
_django_env = os.environ.get("DJANGO_ENV", "")
_render = os.environ.get("RENDER", "")
_loading_prod = bool(_render) or (_django_env == "prod")

# 既存の DJANGO_SETTINGS_MODULE=config.settings を壊さないための互換レイヤ
# Render等の本番は prod、それ以外は dev を既定にする
if _loading_prod:
    from .prod import *  # noqa
else:
    from .dev import *  # noqa
