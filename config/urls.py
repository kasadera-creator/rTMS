# config/urls.py
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    path("", RedirectView.as_view(url="/app/dashboard/", permanent=False)),
    path("app/", include(("rtms_app.urls", "rtms_app"), namespace="rtms_app")),
    path("admin/", admin.site.urls),
]
