from django.apps import AppConfig

class RtmsAppConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'rtms_app'
    verbose_name = 'rTMS 管理メニュー'  # ★ここを変更

    def ready(self):
        import rtms_app.signals  # シグナルをインポートして登録