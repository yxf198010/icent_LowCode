"""
URL configuration for Icent_LowCode project.

Design principles:
- API routes (including docs) are language-agnostic → no i18n prefix.
- Web UI routes (e.g., low-code builder) are non-i18n for now.
- Admin, health, media, static, and root redirects are system-level and non-i18n.
"""

from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import RedirectView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView
from health_check.views import MainView

# 导入 lowcode 首页视图
from lowcode.views import index_view

# ==============================
# System & Root Routes (non-i18n)
# ==============================
urlpatterns = [
    path('', index_view, name='home'),  # 根路径直接映射首页
    path('admin/', admin.site.urls),
    path('health/', MainView.as_view(), name='health_check'),
    # 旧路径重定向（避免冲突）
    path('lowcode/', RedirectView.as_view(url='/', permanent=True), name='lowcode-redirect'),
]

# ==============================
# Web UI Routes (non-i18n)
# 修复点：移除重复的 lowcode 导入，避免路由冲突
# ==============================
urlpatterns += [
    path('app/', include('lowcode.urls', namespace='lowcode')),  # 内部路由前缀
]

# ==============================
# API Routes (language-agnostic)
# ==============================
urlpatterns += [
    path('api/v1/', include('lowcode.api.urls', namespace='lowcode_api')),
    path('api/schema/', SpectacularAPIView.as_view(), name='schema'),
    path('api/docs/', SpectacularSwaggerView.as_view(url_name='schema'), name='swagger-ui'),
    path('api/redoc/', SpectacularRedocView.as_view(url_name='schema'), name='redoc'),
    path('api/lowcode/', RedirectView.as_view(url='/api/v1/', permanent=True), name='lowcode-api-redirect'),
]

# ==============================
# Development-only: Media & Debug Toolbar
# ==============================
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

    if 'debug_toolbar' in settings.INSTALLED_APPS:
        import debug_toolbar
        urlpatterns.insert(0, path('__debug__/', include(debug_toolbar.urls)))