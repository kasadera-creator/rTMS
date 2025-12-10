"""
Django settings for config project.
(中略: 変更なし)
"""
from pathlib import Path
import os
import dj_database_url

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent

# (中略: 変更なし)

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/6.0/howto/static-files/

STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_STORAGE = 'whitenoise.storage.CompressedStaticFilesStorage'

# JAZZMIN設定（見た目のカスタマイズ）
JAZZMIN_SETTINGS = {
    "site_title": "rTMS 実施記録 DB",
    "site_header": "笠寺精治寮病院 rTMS", 
    "site_brand": "笠寺精治寮病院 rTMS", 
    "welcome_sign": "rTMS 実施記録システム",
    "copyright": "Kasadera Seichiryo Hospital",
    "site_logo": "img/logo.jpg", 
    "topmenu_links": [
        {"name": "◀ ダッシュボードへ", "url": "dashboard", "permissions": ["auth.view_user"]},
    ],

    # サイドバーのメニュー順序制御
    "order_with_respect_to": [
        "rtms_app.Patient",           # 1. 患者情報
        "rtms_app.TreatmentSession",  # 2. 治療実施
        "rtms_app.Assessment",        # 3. 状態評価
    ],

    # アイコン設定 (FontAwesome)
    "icons": {
        "rtms_app.Patient": "fas fa-user-injured",
        "rtms_app.TreatmentSession": "fas fa-procedures",
        "rtms_app.Assessment": "fas fa-chart-line",
    },
    
    # ★追加: カスタムCSSの読み込み
    "custom_css": "css/admin_custom.css",
}

# ★サイドバーの色を明るくする設定
JAZZMIN_UI_TWEAKS = {
    "theme": "flatly",   # 明るく清潔感のあるテーマ
    "dark_mode_theme": None,
}

# (以下変更なし)
LOGIN_URL = '/admin/login/'
LOGIN_REDIRECT_URL = '/app/dashboard/'
LOGOUT_REDIRECT_URL = '/admin/login/'