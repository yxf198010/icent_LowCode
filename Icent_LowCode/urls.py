"""
URL configuration for Icent_LowCode project.

Design principles:
- API routes (including docs) are language-agnostic → no i18n prefix.
- Web UI routes (e.g., low-code builder) are non-i18n for now.
- Admin, health, media, static, and root redirects are system-level and non-i18n.
- 首页（/）显示 lowcode 应用，/model-config/ 属于 lowcode 路由，Vue 应用挂载到 /form-designer/
"""

from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import RedirectView, TemplateView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView
from health_check.views import MainView
# 导入 lowcode 首页视图（用于根路径显示 lowcode）
from lowcode.views import index_view
from django.views.static import serve
import os

# ==============================
# System & Root Routes (non-i18n)
# ==============================
urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('django.contrib.auth.urls')),
    path('health/', MainView.as_view(), name='health_check'),
    # 根路径 → lowcode 首页（默认首页显示 lowcode，符合需求）
    path('', index_view, name='home'),
]

# ==============================
# Web UI Routes (non-i18n)
# ==============================
urlpatterns += [
    # lowcode 应用路由（挂载在 /app/ 下，包含其所有子路由，包括 /model-config/）
    path('app/', include('lowcode.urls', namespace='lowcode')),

    # 🔥 Vue 应用路由：仅匹配前端页面路径（排除静态资源）
    # 1. 前端首页：/form-designer/
    path('form-designer/', TemplateView.as_view(template_name='frontend/index.html')),
    # 2. 前端子路由：仅匹配业务子路径（如表单编辑、预览等），不匹配所有路径
    path('form-designer/form-edit/<path:path>/', TemplateView.as_view(template_name='frontend/index.html')),  # 表单编辑
    path('form-designer/preview/<path:path>/', TemplateView.as_view(template_name='frontend/index.html')),  # 表单预览（扩展用）
    path('form-designer/setting/<path:path>/', TemplateView.as_view(template_name='frontend/index.html')),  # 表单设置（扩展用）
    # 👉 后续新增前端子路由，需手动添加（避免用 <path:path> 匹配所有路径，防止拦截静态资源）
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
# Development-only: Media & Static & Debug Toolbar
# ==============================
if settings.DEBUG:
    # 开发环境：提供媒体文件服务
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

    # 开发环境：提供静态文件服务（关键！确保 Vue 静态资源能被正确访问）
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

    # Debug Toolbar（保留原有）
    if 'debug_toolbar' in settings.INSTALLED_APPS:
        import debug_toolbar

        urlpatterns.insert(0, path('__debug__/', include(debug_toolbar.urls)))