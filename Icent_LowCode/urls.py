"""
URL configuration for Icent_LowCode project.

Design principles:
- API routes (including docs) are language-agnostic → no i18n prefix.
- Web UI routes (e.g., low-code builder) are non-i18n for now.
- Admin, health, media, static, and root redirects are system-level and non-i18n.
- 首页（/）显示 lowcode 应用，/model-config/ 属于 lowcode 路由，
  Vue 应用挂载到 /lowcode/designer/ 和 /lowcode_designer/（兼容旧路径）
"""

from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import RedirectView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView
from health_check.views import MainView
# 补充缺失的导入：生产环境静态文件服务
from django.views.static import serve

# 导入视图
from lowcode.views import index_view, designer_view
from lowcode.views.dynamic_model import (
    dynamic_model_detail,  # 模型配置详情（函数视图）
    dynamic_model_data,  # 模型数据列表（函数视图）
    DynamicModelDetailView  # 模型单条数据详情（类视图）
)

# ==============================
# System & Root Routes (non-i18n)
# ==============================
urlpatterns = [
    path('admin/', admin.site.urls),
    path('accounts/', include('django.contrib.auth.urls')),
    path('health/', MainView.as_view(), name='health_check'),
    path('', index_view, name='home'),  # 根路径 → lowcode 首页
]

# ==============================
# Web UI Routes (non-i18n)
# ==============================
urlpatterns += [
    # lowcode 应用主路由（包含 /model-config/ 等）
    path('app/', include('lowcode.urls', namespace='lowcode')),

    # 🔥 新路径：/lowcode/designer/ （推荐路径）
    path('lowcode/designer/', designer_view, name='lowcode-designer-home'),
    re_path(
        r'^lowcode/designer/(?!assets/|api/).*$',
        designer_view,
        name='lowcode-designer-route'
    ),

    # 🔥 兼容旧路径：/lowcode_designer/ （可选保留）
    path('lowcode_designer/', designer_view, name='lowcode_designer-home'),
    re_path(
        r'^lowcode_designer/(?!assets/|api/).*$',
        designer_view,
        name='lowcode_designer-route'
    ),

    # 🌟 关键修正1：统一参数名为 model_name（与视图函数参数一致）
    # 模型配置详情（替换原 model_slug 为 model_name）
    path('lowcode/model/<str:model_name>/', dynamic_model_detail, name='dynamic-model-detail'),
    # 模型数据列表（替换原 model_slug 为 model_name）
    path('lowcode/model/<str:model_name>/data/', dynamic_model_data, name='dynamic-model-data'),
    # 模型单条数据详情（类视图，保持 model_name + pk 参数）
    path('lowcode/model/<str:model_name>/data/<int:pk>/', DynamicModelDetailView.as_view(),
         name='dynamic-model-data-detail'),
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
# Development-only: Media, Static, Debug Toolbar
# ==============================
if settings.DEBUG:
    # 开发环境：自动提供 MEDIA 和 STATIC 文件（包括 lowcode/static/）
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

    if 'debug_toolbar' in settings.INSTALLED_APPS:
        import debug_toolbar

        urlpatterns.insert(0, path('__debug__/', include(debug_toolbar.urls)))

# ==============================
# ⚠️ 仅用于本地测试 DEBUG=False 的情况（非生产！）
# ==============================
# 如果你在本地测试生产模式（DEBUG=False），但没有 Nginx，
# 可临时取消注释以下代码以提供静态文件。
# 上线时务必删除或注释掉！
#
if not settings.DEBUG:
    urlpatterns += [
        re_path(
            r'^static/(?P<path>.*)$',
            serve,
            {'document_root': settings.STATIC_ROOT, 'show_indexes': False},
            name='static-files-for-debug-off'
        ),
        # 补充：生产模式下的媒体文件服务（如需）
        re_path(
            r'^media/(?P<path>.*)$',
            serve,
            {'document_root': settings.MEDIA_ROOT, 'show_indexes': False},
            name='media-files-for-debug-off'
        ),
    ]