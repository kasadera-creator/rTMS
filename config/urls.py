from django.contrib import admin
from django.urls import path, include
from django.views.generic import RedirectView

urlpatterns = [
    # トップページに来たらダッシュボードへ転送
    path('', RedirectView.as_view(url='/app/dashboard/', permanent=False)),
    
    # アプリのURLを '/app/' 以下に割り当て
    path('app/', include('rtms_app.urls')),
    
    path('admin/', admin.site.urls),
]