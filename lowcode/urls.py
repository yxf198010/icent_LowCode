from django.urls import path, include
from django.views.generic import RedirectView
from django.views.static import serve  # 新增：用于静态文件服务
from rest_framework.routers import DefaultRouter
from rest_framework.permissions import (
    IsAuthenticated, IsAdminUser, BasePermission, SAFE_METHODS
)
from rest_framework.decorators import permission_classes, api_view
from rest_framework.response import Response
from django.conf import settings
from django.shortcuts import render  # 新增：导入模板渲染函数
from . import views
import datetime
import os  # 新增：用于路径处理

app_name = 'lowcode'  # 严格统一命名空间，全项目复用

# ==============================
# 自定义权限类（精细化权限控制）
# 优化点：补充权限说明、完善边界处理
# ==============================
class IsModelAdminOrReadOnly(BasePermission):
    """
    模型配置权限：
    - 管理员（staff）：可执行所有操作（GET/POST/PUT/DELETE）
    - 普通登录用户：仅可读取（GET/HEAD/OPTIONS）
    - 未登录用户：无权限
    """
    def has_permission(self, request, view):
        # 未登录用户直接拒绝
        if not request.user or not request.user.is_authenticated:
            return False
        # 安全方法（读取）：所有登录用户可访问
        if request.method in SAFE_METHODS:
            return True
        # 写操作：仅管理员可访问
        return request.user.is_staff

class IsDataOwnerOrAdmin(BasePermission):
    """
    数据操作权限：
    - 管理员（staff）：可操作所有数据
    - 普通登录用户：仅可操作自己创建的数据（需模型有 created_by 字段）
    - 未登录用户：无权限
    """
    def has_object_permission(self, request, view, obj):
        # 管理员直接放行
        if request.user.is_staff:
            return True
        # 检查数据是否属于当前用户（容错处理）
        try:
            return hasattr(obj, 'created_by') and obj.created_by == request.user
        except Exception:
            return False

# ==============================
# LowcodeDesigner 相关视图与路由配置（优化路径匹配）
# ==============================
# 1. favicon.ico 访问视图（适配前端图标）
def favicon_view(request):
    """提供 favicon.ico 静态文件访问"""
    favicon_path = 'lowcode_designer/favicon.ico'
    # 适配开发/生产环境的静态文件目录
    document_root = settings.STATICFILES_DIRS[0] if settings.DEBUG else settings.STATIC_ROOT
    return serve(request, favicon_path, document_root=document_root)

# 2. LowcodeDesigner 前端页面视图（渲染构建后的模板）
def lowcode_designer_view(request):
    """渲染 LowcodeDesigner 前端构建后的入口模板"""
    return render(request, 'lowcode_designer/index.html')

# ==============================
# DRF API 路由配置（仅保留已实现的视图）
# 优化点：添加注释说明、统一路由命名规范
# ==============================
router = DefaultRouter()

# 1. 方法调用日志视图集（管理员专属，敏感操作审计）
if hasattr(views, 'DynamicMethodCallLogViewSet'):
    admin_viewset = permission_classes([IsAdminUser])(views.DynamicMethodCallLogViewSet)
    router.register(
        r"method-call-logs",
        admin_viewset,
        basename="method-call-log"
    )

# 2. 数据权限视图集（登录用户可管理自己的权限）
if hasattr(views, 'DataPermissionViewSet'):
    permission_viewset = permission_classes([IsAuthenticated])(views.DataPermissionViewSet)
    router.register(
        r"data-permissions",
        permission_viewset,
        basename="data-permission"
    )

# ==============================
# API 子路由分组（移除未实现的依赖）
# ==============================
api_urlpatterns = [
    # API根路径文档（所有登录用户可访问）
    path("", views.APIRootView.as_view(), name='api-root'),
    # 基础CRUD API（由router自动生成）
    path("", include(router.urls)),
]

# ------------------------------
# 批量权限管理（管理员专属）- 条件添加路由
# ------------------------------
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

# ------------------------------
# 日志导出功能（登录用户可访问）- 条件添加路由
# ------------------------------
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
        path("download-export/<str:task_id>/",
             permission_classes([IsAuthenticated])(views.DownloadExportView.as_view()),
             name="download-export")
    )

# ------------------------------
# 工具API（管理员专属）- 条件添加路由
# ------------------------------
if hasattr(views, 'refresh_methods'):
    api_urlpatterns.append(
        path("tools/refresh-methods/",
             permission_classes([IsAdminUser])(views.refresh_methods),
             name="refresh-dynamic-methods")
    )

# ==============================
# 主URL配置（规范稳定版）
# ==============================
urlpatterns = [
    # ------------------------------
    # LowcodeDesigner 相关路由（优化命名与路径）
    # ------------------------------
    path('favicon.ico', favicon_view, name='favicon'),  # 图标访问路由
    path('designer/', lowcode_designer_view, name='lowcode_designer'),  # 设计器入口
    path('designer/<path:path>', lowcode_designer_view),  # history 模式支持（捕获所有子路径）

    # ------------------------------
    # 核心：首页配置（根路径直接映射）
    # ------------------------------
    path('', views.index_view, name='index'),  # 根路径 → 首页（核心入口）
    path('home/', RedirectView.as_view(pattern_name='lowcode:index'), name='home-redirect'),  # 兼容旧路径
    path('index/', RedirectView.as_view(pattern_name='lowcode:index'), name='index-redirect'),  # 避免重复路由

    # ------------------------------
    # 动态模型管理（核心业务模块）
    # ------------------------------
    # 模型列表（登录可见，管理员可编辑）
    path('models/',
         permission_classes([IsModelAdminOrReadOnly])(views.model_list_view),
         name='model-list'),

    # 模型数据CRUD（精细化权限控制）
    path('models/<str:model_name>/',
         permission_classes([IsAuthenticated])(views.DynamicModelListView.as_view()),
         name='dynamic-model-list'),  # 列表页
    path('models/<str:model_name>/add/',
         permission_classes([IsAuthenticated])(views.DynamicModelCreateView.as_view()),
         name='dynamic-model-create'),  # 新增页
    path('models/<str:model_name>/<int:pk>/',
         permission_classes([IsDataOwnerOrAdmin])(views.DynamicModelDetailView.as_view()),
         name='dynamic-model-detail'),  # 详情页
    path('models/<str:model_name>/<int:pk>/edit/',
         permission_classes([IsDataOwnerOrAdmin])(views.DynamicModelUpdateView.as_view()),
         name='dynamic-model-update'),  # 编辑页
    path('models/<str:model_name>/<int:pk>/delete/',
         permission_classes([IsDataOwnerOrAdmin | IsAdminUser])(views.DynamicModelDeleteView.as_view()),
         name='dynamic-model-delete'),  # 删除页

    # ------------------------------
    # API接口入口（版本化管理）
    # ------------------------------
    path('api/v1/', include(api_urlpatterns)),  # API v1版本，便于后续迭代
    path('api/', RedirectView.as_view(pattern_name='lowcode:api-root'), name='api-redirect'),  # API根路径重定向
    path('api/v1/', RedirectView.as_view(pattern_name='lowcode:api-root'), name='api-v1-redirect'),  # 避免重复
]

# ------------------------------
# 系统管理（权限敏感模块）- 条件添加路由
# ------------------------------
if hasattr(views, 'dashboard_view'):
    urlpatterns.append(
        path('system/dashboard/',
             permission_classes([IsAuthenticated])(views.dashboard_view),
             name='dashboard')
    )

if hasattr(views, 'model_upgrade_view'):
    urlpatterns.append(
        path('system/model-upgrade/',
             permission_classes([IsAdminUser])(views.model_upgrade_view),
             name='model-upgrade')  # 高危操作，仅管理员
    )

if hasattr(views, 'prometheus_metrics'):
    urlpatterns.append(
        path('system/metrics/',
             permission_classes([IsAdminUser])(views.prometheus_metrics),
             name='prometheus-metrics')  # 监控指标，仅管理员
    )

# ------------------------------
# 工具功能（辅助模块）- 条件添加路由
# ------------------------------
if hasattr(views, 'refresh_methods'):
    urlpatterns.append(
        path('tools/refresh-methods/',
             permission_classes([IsAdminUser])(views.refresh_methods),
             name='refresh-methods')
    )

if hasattr(views, 'call_dynamic_method'):
    urlpatterns.append(
        path('tools/models/<str:model_name>/<int:instance_id>/methods/<str:method_name>/call/',
             permission_classes([IsAdminUser])(views.call_dynamic_method),
             name='call-dynamic-method')
    )

# ------------------------------
# 演示功能（生产环境建议移除）- 条件添加路由
# ------------------------------
if hasattr(views, 'create_lowcode_user_example'):
    urlpatterns.append(
        path('demo/create-user/',
             permission_classes([IsAdminUser])(views.create_lowcode_user_example),
             name='demo-create-user')
    )
if hasattr(views, 'get_lowcode_user_detail'):
    urlpatterns.append(
        path('demo/users/<int:user_id>/',
             permission_classes([IsAdminUser])(views.get_lowcode_user_detail),
             name='demo-get-user')
    )

# ==============================
# 补充配置（增强稳定性与可用性）
# ==============================
# 1. 全局API根路径文档视图（确保存在，避免启动报错）
if not hasattr(views, 'APIRootView'):
    from rest_framework.views import APIView

    class APIRootView(APIView):
        """Icent低代码平台API接口根文档（v1）"""
        permission_classes = [IsAuthenticated]
        description = "提供动态模型配置、数据管理、权限控制、日志导出等核心功能"

        def get(self, request):
            # 动态构建绝对URL，适配不同部署环境
            base_url = request.build_absolute_uri('/api/v1/')
            return Response({
                "message": "Icent低代码平台API接口文档",
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

    # 动态添加到views，避免重复定义
    setattr(views, 'APIRootView', APIRootView)

# 2. 自定义404/500页面路由（优化用户体验，兼容缺失场景）
if hasattr(views, 'custom_404_view'):
    handler404 = 'lowcode.views.custom_404_view'
else:
    handler404 = 'django.views.defaults.page_not_found'

if hasattr(views, 'custom_500_view'):
    handler500 = 'lowcode.views.custom_500_view'
else:
    handler500 = 'django.views.defaults.server_error'

# 3. 健康检查接口（独立实现，不依赖外部视图，便于监控）
@api_view(['GET'])
@permission_classes([IsAdminUser])
def api_health_check(request):
    """API健康检查接口（管理员专属）- 用于服务监控"""
    return Response({
        "status": "healthy",
        "service": "dynamic-model-system",
        "api_version": "v1",
        "django_version": settings.DJANGO_VERSION,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),  # 使用UTC时间，统一时区
        "environment": settings.ENVIRONMENT if hasattr(settings, 'ENVIRONMENT') else "production"
    })

# 添加健康检查路由（确保不重复添加）
health_check_route = path('api/v1/health/', api_health_check, name='api-health-check')
if health_check_route not in urlpatterns:
    urlpatterns.append(health_check_route)

# ==============================
# 开发环境专属配置（仅DEBUG模式生效）
# ==============================
if settings.DEBUG:
    # 开发环境下添加API调试路由（生产环境自动隐藏）
    @api_view(['GET'])
    @permission_classes([IsAdminUser])
    def api_debug_info(request):
        """开发环境调试信息接口"""
        return Response({
            "debug_mode": True,
            "registered_views": {
                "dynamic_method_call_log_viewset": hasattr(views, 'DynamicMethodCallLogViewSet'),
                "data_permission_viewset": hasattr(views, 'DataPermissionViewSet'),
                "batch_data_permission_view": hasattr(views, 'BatchDataPermissionView'),
                "method_log_export_view": hasattr(views, 'MethodLogExportView')
            },
            "api_routes": [str(route.pattern) for route in api_urlpatterns if hasattr(route, 'pattern')]
        })

    urlpatterns.append(
        path('api/v1/debug/', api_debug_info, name='api-debug-info')
    )