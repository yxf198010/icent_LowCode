"""
URL configuration for Icent_LowCode project.

Design principles:
- API routes (including docs) are language-agnostic → no i18n prefix.
- Web UI routes (e.g., low-code builder) are non-i18n for now.
- Admin, health, media, static, and root redirects are system-level and non-i18n.
- 首页（/）显示 lowcode 应用，/model-config/ 属于 lowcode 路由，Vue 应用挂载到 /lowcode_designer/
"""

from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.conf.urls.static import static
from django.views.generic import RedirectView, TemplateView
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView, SpectacularRedocView
from health_check.views import MainView
# 导入 lowcode 首页视图（用于根路径显示 lowcode）
from lowcode.views import index_view
from django.views.static import serve
import os
import json  # 新增：解析Vite manifest.json
from typing import Dict, Optional  # 类型注解（可选，提升代码可读性）


# ==============================
# 核心优化：自定义Vue视图（加载manifest + 传递模板变量）
# ==============================
class VueLowCodeDesignerView(TemplateView):
    """Vue低代码设计器视图"""
    template_name = 'lowcode_designer/index.html'  # 匹配模板路径

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # 适配开发/生产环境的manifest路径
        if settings.DEBUG:
            manifest_dir = os.path.join(settings.STATICFILES_DIRS[0], 'lowcode_designer/.vite')
        else:
            manifest_dir = os.path.join(settings.STATIC_ROOT, 'lowcode_designer/.vite')
        manifest_path = os.path.join(manifest_dir, 'manifest.json')

        # 解析manifest获取哈希化资源路径（可选，用于动态加载资源）
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, 'r', encoding='utf-8') as f:
                    manifest = json.load(f)
                # 假设前端入口文件为 src/main.js，根据实际情况调整
                if 'src/main.js' in manifest:
                    context['js_entry'] = manifest['src/main.js']['file']
                    context['css_entry'] = manifest['src/main.js']['css'][0] if 'css' in manifest['src/main.js'] else ''
            except Exception as e:
                print(f"解析manifest失败: {e}")
        return context


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

    # 🔥 Vue 低代码设计器路由配置（核心优化）
    # 1. 优先处理静态资源：避免前端路由拦截Vue的assets资源
    re_path(
        r'^lowcode_designer/assets/(?P<path>.*)$',  # 精准匹配assets子路径
        serve,
        {
            # 适配开发/生产环境的静态资源根目录
            'document_root': os.path.join(settings.STATICFILES_DIRS[0] if settings.DEBUG else settings.STATIC_ROOT,
                                          'lowcode_designer/assets'),
            'show_indexes': False  # 禁止目录浏览（安全优化）
        },
        name='lowcode_designer-assets'
    ),
    # 2. Vue应用首页：/lowcode_designer/
    path('lowcode_designer/', VueLowCodeDesignerView.as_view(), name='lowcode_designer-home'),
    # 3. 前端路由兜底：匹配所有/lowcode_designer/下的业务子路径
    #    排除assets/api，避免拦截静态资源和接口请求
    re_path(
        r'^lowcode_designer/(?!assets/|api/)(?P<path>.*)$',
        VueLowCodeDesignerView.as_view(),
        name='lowcode_designer-route'
    ),
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
    # 开发环境：媒体文件服务
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

    # 开发环境：静态文件服务（确保Vue资源可访问）
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

    # Debug Toolbar（开发调试用）
    if 'debug_toolbar' in settings.INSTALLED_APPS:
        import debug_toolbar

        urlpatterns.insert(0, path('__debug__/', include(debug_toolbar.urls)))

# ==============================
# 生产环境优化配置
# ==============================
if not settings.DEBUG:
    # 生产环境：lowcode_designer静态资源兜底（覆盖所有子路径）
    urlpatterns += [
        re_path(
            r'^static/lowcode_designer/(?P<path>.*)$',
            serve,
            {
                'document_root': os.path.join(settings.STATIC_ROOT, 'lowcode_designer'),
                'show_indexes': False  # 安全优化：禁止目录浏览
            },
            name='static-lowcode_designer'
        ),
    ]

    # 生产环境：禁用DEBUG_TOOLBAR（安全加固）
    if 'debug_toolbar' in urlpatterns:
        urlpatterns = [p for p in urlpatterns if not p.pattern.match('__debug__/')]

# ==============================
# 额外优化：URL命名空间与注释规范
# ==============================
# 统一命名空间（可选，如需批量反向解析）
app_name = 'icent_lowcode'