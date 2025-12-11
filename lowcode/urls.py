from django.urls import path, include
from django.views.generic import RedirectView
from django.views.static import serve
from rest_framework.routers import DefaultRouter
from rest_framework.permissions import (
    IsAuthenticated, IsAdminUser, BasePermission, SAFE_METHODS
)
from rest_framework.decorators import permission_classes, api_view
from rest_framework.response import Response
from django.conf import settings
from django.shortcuts import render
from . import views
import datetime
import os

app_name = 'lowcode'  # 严格统一命名空间，全项目复用


# ==============================
# 自定义权限类（精细化权限控制）
# ==============================
class IsModelAdminOrReadOnly(BasePermission):
    """
    模型配置权限：
    - 管理员（staff）：可执行所有操作（GET/POST/PUT/DELETE）
    - 普通登录用户：仅可读取（GET/HEAD/OPTIONS）
    - 未登录用户：无权限
    """

    def has_permission(self, request, view):
        if not request.user or not request.user.is_authenticated:
            return False
        if request.method in SAFE_METHODS:
            return True
        return request.user.is_staff


class IsDataOwnerOrAdmin(BasePermission):
    """
    数据操作权限：
    - 管理员（staff）：可操作所有数据
    - 普通登录用户：仅可操作自己创建的数据（需模型有 created_by 字段）
    - 未登录用户：无权限
    """

    def has_object_permission(self, request, view, obj):
        if request.user.is_staff:
            return True
        try:
            return hasattr(obj, 'created_by') and obj.created_by == request.user
        except Exception:
            return False


# ==============================
# LowcodeDesigner 视图（使用 Vite 资源工具函数）
# ==============================
from django.views import View
from lowcode.utils.vite import get_vite_asset


class DesignerView(View):
    """低代码设计器页面视图，自动注入 Vite 构建后的 JS/CSS 资源"""

    def get(self, request, path=None):  # path 用于 history 模式捕获
        assets = get_vite_asset(app_name="lowcode_designer")
        context = {
            "vite_js": assets["js"],
            "vite_css": assets["css"],
        }
        return render(request, "lowcode/designer.html", context)


# ==============================
# favicon.ico 访问视图（适配前端图标）
# ==============================
def favicon_view(request):
    """提供 favicon.ico 静态文件访问"""
    # 注意：Vite 构建后，favicon 通常被复制到 assets/ 目录下
    favicon_path = 'lowcode_designer/assets/favicon.ico'
    document_root = settings.STATICFILES_DIRS[0] if settings.DEBUG else settings.STATIC_ROOT
    return serve(request, favicon_path, document_root=document_root)


# ==============================
# DRF API 路由配置（仅保留已实现的视图）
# ==============================
router = DefaultRouter()

if hasattr(views, 'DynamicMethodCallLogViewSet'):
    admin_viewset = permission_classes([IsAdminUser])(views.DynamicMethodCallLogViewSet)
    router.register(
        r"method-call-logs",
        admin_viewset,
        basename="method-call-log"
    )

if hasattr(views, 'DataPermissionViewSet'):
    permission_viewset = permission_classes([IsAuthenticated])(views.DataPermissionViewSet)
    router.register(
        r"data-permissions",
        permission_viewset,
        basename="data-permission"
    )


# ==============================
# API 子路由分组
# ==============================
api_urlpatterns = [
    path("", views.APIRootView.as_view(), name='api-root'),
    path("", include(router.urls)),
]

# 批量权限管理
if hasattr(views, 'BatchDataPermissionView'):
    api_urlpatterns.append(
        path("batch-data-permission/",
             permission_classes([IsAdminUser])(views.BatchDataPermissionView.as_view()),
             name="batch-data-permission")
    )
if hasattr(views, 'BatchRevokeDataPermissionView'):
    api_urlpatterns.append(
        path("batch-revoke-data-permission/",
             permission_classes([IsAdminUser])(views.BatchRevokeDataPermissionView.as_view()),
             name="batch-revoke-data-permission")
    )

# 日志导出
if hasattr(views, 'MethodLogExportView'):
    api_urlpatterns.append(
        path("export-method-logs/",
             permission_classes([IsAuthenticated])(views.MethodLogExportView.as_view()),
             name="export-method-logs")
    )
if hasattr(views, 'AsyncExportMethodLogView'):
    api_urlpatterns.append(
        path("async-export-method-logs/",
             permission_classes([IsAuthenticated])(views.AsyncExportMethodLogView.as_view()),
             name="async-export-method-logs")
    )
if hasattr(views, 'ExportProgressView'):
    api_urlpatterns.append(
        path("export-progress/",
             permission_classes([IsAuthenticated])(views.ExportProgressView.as_view()),
             name="export-progress")
    )
if hasattr(views, 'DownloadExportView'):
    api_urlpatterns.append(
        path("download-export/",
             permission_classes([IsAuthenticated])(views.DownloadExportView.as_view()),
             name="download-export")
    )

# 工具API
if hasattr(views, 'refresh_methods'):
    api_urlpatterns.append(
        path("tools/refresh-methods/",
             permission_classes([IsAdminUser])(api_view(['POST'])(views.refresh_methods)),
             name="refresh-dynamic-methods")
    )


# ==============================
# 主URL配置
# ==============================
urlpatterns = [
    # LowcodeDesigner 核心路由（使用新 DesignerView）
    path('favicon.ico', favicon_view, name='favicon'),
    path('designer/', DesignerView.as_view(), name='lowcode_designer'),
    path('designer/<path:path>', DesignerView.as_view()),  # history 模式支持

    # 首页
    path('', views.index_view, name='index'),
    path('home/', RedirectView.as_view(pattern_name='lowcode:index'), name='home-redirect'),
    path('index/', RedirectView.as_view(pattern_name='lowcode:index'), name='index-redirect'),

    # 动态模型管理
    path('models/',
         permission_classes([IsModelAdminOrReadOnly])(api_view(['GET'])(views.model_list_view)),
         name='model-list'),
    path('models/<str:model_name>/',
         permission_classes([IsAuthenticated])(views.DynamicModelListView.as_view()),
         name='dynamic-model-list'),
    path('models/<str:model_name>/add/',
         permission_classes([IsAuthenticated])(views.DynamicModelCreateView.as_view()),
         name='dynamic-model-create'),
    path('models/<str:model_name>/<int:pk>/',
         permission_classes([IsDataOwnerOrAdmin])(views.DynamicModelDetailView.as_view()),
         name='dynamic-model-detail'),
    path('models/<str:model_name>/<int:pk>/edit/',
         permission_classes([IsDataOwnerOrAdmin])(views.DynamicModelUpdateView.as_view()),
         name='dynamic-model-update'),
    path('models/<str:model_name>/<int:pk>/delete/',
         permission_classes([IsDataOwnerOrAdmin | IsAdminUser])(views.DynamicModelDeleteView.as_view()),
         name='dynamic-model-delete'),

    # API
    path('api/v1/', include(api_urlpatterns)),
    path('api/', RedirectView.as_view(pattern_name='lowcode:api-root'), name='api-redirect'),
    path('api/v1/', RedirectView.as_view(pattern_name='lowcode:api-root'), name='api-v1-redirect'),
]


# ==============================
# 系统管理 & 工具 & 演示（条件路由）
# ==============================
system_routes = []

# 系统管理
if hasattr(views, 'dashboard_view'):
    system_routes.append(
        path('system/dashboard/',
             permission_classes([IsAuthenticated])(api_view(['GET'])(views.dashboard_view)),
             name='dashboard')
    )
if hasattr(views, 'model_upgrade_view'):
    system_routes.append(
        path('system/model-upgrade/',
             permission_classes([IsAdminUser])(api_view(['GET'])(views.model_upgrade_view)),
             name='model-upgrade')
    )
if hasattr(views, 'prometheus_metrics'):
    system_routes.append(
        path('system/metrics/',
             permission_classes([IsAdminUser])(api_view(['GET'])(views.prometheus_metrics)),
             name='prometheus-metrics')
    )

# 工具功能
if hasattr(views, 'refresh_methods'):
    system_routes.append(
        path('tools/refresh-methods/',
             permission_classes([IsAdminUser])(api_view(['POST'])(views.refresh_methods)),
             name='refresh-methods')
    )
if hasattr(views, 'call_dynamic_method'):
    system_routes.append(
        path('tools/models/<str:model_name>/<int:instance_id>/methods/<str:method_name>/call/',
             permission_classes([IsAdminUser])(api_view(['POST'])(views.call_dynamic_method)),
             name='call-dynamic-method')
    )

# 演示功能
if hasattr(views, 'create_lowcode_user_example'):
    system_routes.append(
        path('demo/create-user/',
             permission_classes([IsAdminUser])(api_view(['GET'])(views.create_lowcode_user_example)),
             name='demo-create-user')
    )
if hasattr(views, 'get_lowcode_user_detail'):
    system_routes.append(
        path('demo/users/<int:user_id>/',
             permission_classes([IsAdminUser])(api_view(['GET'])(views.get_lowcode_user_detail)),
             name='demo-get-user')
    )

urlpatterns.extend(system_routes)


# ==============================
# 补充配置
# ==============================
# 确保 APIRootView 存在
if not hasattr(views, 'APIRootView'):
    from rest_framework.views import APIView

    class APIRootView(APIView):
        permission_classes = [IsAuthenticated]
        description = "提供动态模型配置、数据管理、权限控制、日志导出等核心功能"

        def get(self, request):
            base_url = request.build_absolute_uri('/api/v1/')
            return Response({
                "message": "Icent AI原生低代码平台API接口文档",
                "version": "1.0.0",
                "description": self.description,
                "core-endpoints": {
                    "模型列表": f"{base_url}models/",
                    "方法调用日志": f"{base_url}method-call-logs/",
                    "数据权限管理": f"{base_url}data-permissions/",
                    "API健康检查": f"{base_url}health/"
                },
                "security-notice": "生产环境建议：1.限制API访问IP；2.启用HTTPS；3.定期审计敏感操作日志",
                "contact": "技术支持：admin@example.com"
            })

    setattr(views, 'APIRootView', APIRootView)


# 自定义错误页面
if hasattr(views, 'custom_404_view'):
    handler404 = 'lowcode.views.custom_404_view'
else:
    handler404 = 'django.views.defaults.page_not_found'

if hasattr(views, 'custom_500_view'):
    handler500 = 'lowcode.views.custom_500_view'
else:
    handler500 = 'django.views.defaults.server_error'


# 健康检查
@api_view(['GET'])
@permission_classes([IsAdminUser])
def api_health_check(request):
    return Response({
        "status": "healthy",
        "service": "dynamic-model-system",
        "api_version": "v1",
        "django_version": settings.django_version,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "environment": getattr(settings, 'ENVIRONMENT', "production")
    })


# 避免重复添加健康检查路由
health_route_exists = any(
    hasattr(route, 'name') and route.name == 'api-health-check'
    for route in urlpatterns
)
if not health_route_exists:
    urlpatterns.append(path('api/v1/health/', api_health_check, name='api-health-check'))


# ==============================
# 开发环境专属配置
# ==============================
if settings.DEBUG:
    @api_view(['GET'])
    @permission_classes([IsAdminUser])
    def api_debug_info(request):
        api_routes = []
        for route in api_urlpatterns:
            if hasattr(route, 'pattern'):
                api_routes.append(str(route.pattern))

        designer_route_exists = any(
            hasattr(route, 'name') and route.name == 'lowcode_designer'
            for route in urlpatterns
        )

        return Response({
            "debug_mode": True,
            "registered_views": {
                "dynamic_method_call_log_viewset": hasattr(views, 'DynamicMethodCallLogViewSet'),
                "data_permission_viewset": hasattr(views, 'DataPermissionViewSet'),
                "batch_data_permission_view": hasattr(views, 'BatchDataPermissionView'),
                "method_log_export_view": hasattr(views, 'MethodLogExportView'),
                "lowcode_designer_view": True  # 现在使用 DesignerView，始终存在
            },
            "api_routes": api_routes,
            "designer_route": "designer/" if designer_route_exists else "missing"
        })

    urlpatterns.append(path('api/v1/debug/', api_debug_info, name='api-debug-info'))

