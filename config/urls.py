# config/urls.py
from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

urlpatterns = [
    path("", RedirectView.as_view(url="/app/dashboard/", permanent=False)),
    path("app/", include(("rtms_app.urls", "rtms_app"), namespace="rtms_app")),
    # Register print routes under the 'print' namespace
    path("app/print/", include(("rtms_app.print_urls", "print"), namespace="print")),
    path("admin/", admin.site.urls),
]
