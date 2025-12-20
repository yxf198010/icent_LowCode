# 非动态模型代码（首页、仪表盘、日志导出、权限管理、用户管理、监控指标等）
import logging
import os
import re
import json
from datetime import datetime, date
from uuid import uuid4
from typing import Any, Dict, List, Type, Optional

from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, models
from django.db.models import Q
from django.http import Http404, HttpResponse, FileResponse, JsonResponse, HttpRequest
from django.shortcuts import render, get_object_or_404
from django.urls import reverse
from django.views.generic import ListView, CreateView, DetailView, UpdateView, DeleteView
from django import forms
from django.contrib.admin.views.decorators import staff_member_required
from django.core.cache import cache
from django.utils.text import capfirst
from django.utils import timezone
from django.template.response import TemplateResponse
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from rest_framework.viewsets import ReadOnlyModelViewSet
from rest_framework.filters import SearchFilter, OrderingFilter
from rest_framework.pagination import PageNumberPagination
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.core.files.storage import default_storage
from lowcode.utils.vite import get_vite_asset

# Local imports
from lowcode.models.models import DataPermission, LowCodeMethodCallLog, LowCodeUser, Role
from lowcode.api.serializers import (
    LowCodeMethodCallLogSerializer,
    BatchDataPermissionSerializer,
    BatchRevokeDataPermissionSerializer,
)
from lowcode.io.excel import generate_method_log_excel
from lowcode.tasks import async_export_method_log
import django

# Config
ALLOWED_MODELS = getattr(settings, 'LOWCODE_ALLOWED_MODELS', None)
EXPORT_MAX_RECORDS = getattr(settings, 'LOWCODE_EXPORT_MAX_RECORDS', 50000)
EXPORT_SUBDIR = getattr(settings, 'LOWCODE_EXPORT_SUBDIR', 'lowcode_exports/').rstrip('/') + '/'
CACHE_TIMEOUT = 60  # 统一缓存超时时间

logger = logging.getLogger(__name__)

# ========== Type Hints ==========
ModelConfigType = Dict[str, Any]
OverviewDataType = Dict[str, Any]

# ========== 通用工具函数 ==========
def _sanitize_filename(filename: str) -> str:
    """清理文件名，避免非法字符"""
    if not re.match(r'^[a-zA-Z0-9._\-]+$', filename):
        raise ValidationError("文件名仅允许字母、数字、下划线、连字符和点")
    return filename

def _apply_log_filters(queryset, params: Dict[str, Any]):
    """应用日志过滤条件（优化过滤逻辑）"""
    filters = Q()
    if params.get("user"):
        filters &= Q(user_id=params["user"])
    if params.get("model_name"):
        filters &= Q(model_name__icontains=params["model_name"])
    if params.get("method_name"):
        filters &= Q(method_name__icontains=params["method_name"])
    if params.get("result_status"):
        filters &= Q(result_status=params["result_status"])
    if params.get("start_time"):
        filters &= Q(call_time__gte=params["start_time"])
    if params.get("end_time"):
        filters &= Q(call_time__lte=params["end_time"])
    return queryset.filter(filters) if filters else queryset

# ========== 核心页面视图 ==========
def designer_view(request):
    """设计器视图"""
    context = {}
    if not (settings.DEBUG and getattr(settings, 'VITE_DEV_MODE', True)):
        try:
            context['vite_assets'] = get_vite_asset(app_name="lowcode_designer")
        except Exception as e:
            logger.error("Vite asset loading failed", exc_info=True)
            if settings.DEBUG:
                raise
    return render(request, 'lowcode/designer.html', context)

def index_view(request: HttpRequest):
    """首页视图（优化版，对应 index.html 模板）
    功能：显示动态模型统计、最近模型列表、快速操作入口所需数据
    """
    from lowcode.views.dynamic_model import get_all_dynamic_model_configs, get_model_record_count
    try:
        # 获取所有模型配置（带缓存优化）
        model_configs = get_all_dynamic_model_configs()

        # 计算首页统计数据（用于数据概览区域）
        total_models = len(model_configs)
        created_table_count = sum(1 for cfg in model_configs if cfg['exists_in_db'])
        total_fields = sum(cfg['field_count'] for cfg in model_configs)
        total_records = sum(cfg['record_count'] for cfg in model_configs if cfg['record_count'] != -1)

        # 获取 Django 版本
        django_version = django.get_version()

        # 组装上下文数据（适配 index.html 模板需求）
        context = {
            'model_configs': model_configs,
            'current_time': timezone.now(),
            'total_models': total_models,
            'created_table_count': created_table_count,
            'total_fields': total_fields,
            'total_records': total_records,
            'django_version': django_version,
            'title': '系统首页 - Icent AI原生低代码平台',
        }

        return render(request, 'index.html', context)

    except Exception as e:
        logger.exception("首页加载失败")
        messages.error(request, "系统异常，首页数据加载失败，请联系管理员")
        # 异常时返回基础页面，避免白屏
        return render(request, 'index.html', {
            'model_configs': [],
            'current_time': timezone.now(),
            'total_models': 0,
            'created_table_count': 0,
            'total_fields': 0,
            'total_records': 0,
            'title': '系统首页 - Icent AI原生低代码平台',
        })

def dashboard_view(request: HttpRequest):
    """仪表盘视图（优化统计逻辑）"""
    static_names = {'DataPermission', 'LowCodeMethodCallLog', 'LowCodeModelConfig', 'FieldModel', 'LowCodeUser', 'Role'}
    app_models = [m for m in apps.get_app_config('lowcode').get_models() if m.__name__ not in static_names]

    # 优化统计性能
    total_models = len(app_models)
    total_data = 0

    # 使用缓存批量统计
    for model in app_models:
        cache_key = f'lowcode_data_count_{model.__name__}'
        cnt = cache.get(cache_key)
        if cnt is None:
            try:
                cnt = model.objects.count()
                cache.set(cache_key, cnt, CACHE_TIMEOUT)
            except Exception as e:
                logger.warning(f"统计模型 '{model.__name__}' 记录数失败: {str(e)}")
                cnt = 0
        total_data += cnt

    overview_data: List[OverviewDataType] = [
        {
            'label': '动态模型总数',
            'value': total_models,
            'color': 'primary',
            'icon': 'bi-diagram-3',
            'url': reverse('lowcode:model-list'),  # 对应优化后的路由名称（kebab-case）
            'action_text': '前往管理'
        },
        {
            'label': '累计数据条数',
            'value': total_data,
            'color': 'success',
            'icon': 'bi-database',
            'url': reverse('lowcode:model-list'),  # 对应优化后的路由名称（kebab-case）
            'action_text': '查看详情'
        },
    ]

    return render(request, 'lowcode/dashboard.html', {
        'overview_data': overview_data,
        'title': 'Icent AI原生低代码平台 - 仪表盘'
    })

# ========== API Views ==========
class StandardResultsSetPagination(PageNumberPagination):
    """标准分页配置"""
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100

class DynamicMethodCallLogViewSet(ReadOnlyModelViewSet):
    """方法调用日志API视图集"""
    serializer_class = LowCodeMethodCallLogSerializer
    permission_classes = [IsAdminUser]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = {
        "user": ["exact"],
        "model_name": ["exact", "icontains"],
        "method_name": ["exact", "icontains"],
        "result_status": ["exact"],
    }
    search_fields = ["model_name", "method_name", "exception_msg"]
    ordering_fields = ["call_time", "time_cost"]
    ordering = ["-call_time"]
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        queryset = LowCodeMethodCallLog.objects.all()
        params = {
            "start_time": self.request.query_params.get("start_time"),
            "end_time": self.request.query_params.get("end_time")
        }
        return _apply_log_filters(queryset, params)

class BatchDataPermissionView(APIView):
    """批量数据权限授权API"""
    permission_classes = [IsAdminUser]

    def post(self, request):
        serializer = BatchDataPermissionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # 去重处理，避免重复授权
        existing_perms = DataPermission.objects.filter(
            user_id__in=data["user_ids"],
            model_name=data["model_name"],
            data_id__in=data["data_ids"]
        ).values_list('user_id', 'data_id')
        existing_set = set(existing_perms)

        objs = []
        for u in data["user_ids"]:
            for d in data["data_ids"]:
                if (u, d) not in existing_set:
                    objs.append(DataPermission(user_id=u, model_name=data["model_name"], data_id=d))

        if objs:
            DataPermission.objects.bulk_create(objs, batch_size=100)

        return Response({
            "code": 200,
            "msg": f"成功授权{len(data['user_ids'])}个用户访问{len(data['data_ids'])}条{data['model_name']}数据（新增{len(objs)}条权限）",
            "data": {
                "user_ids": data["user_ids"],
                "data_ids": data["data_ids"],
                "model_name": data["model_name"],
                "total_count": len(objs),
                "duplicate_count": len(data["user_ids"]) * len(data["data_ids"]) - len(objs)
            }
        })

class MethodLogExportView(APIView):
    """方法调用日志导出API"""
    permission_classes = [IsAdminUser]

    def get(self, request):
        queryset = LowCodeMethodCallLog.objects.all()
        params = {k: request.query_params.get(k) for k in
                  ["user", "model_name", "method_name", "result_status", "start_time", "end_time"]}
        queryset = _apply_log_filters(queryset, params)

        record_count = queryset.count()
        if record_count > EXPORT_MAX_RECORDS:
            return Response({
                "code": 400,
                "msg": f"导出记录数超过最大限制 ({EXPORT_MAX_RECORDS})，当前{record_count}条",
                "data": {"max_records": EXPORT_MAX_RECORDS, "current_count": record_count}
            }, status=400)

        try:
            excel_buffer = generate_method_log_excel(queryset)
            response = HttpResponse(
                excel_buffer.getvalue(),
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            current_date = datetime.now().strftime("%Y%m%d_%H%M%S")
            response["Content-Disposition"] = f'attachment; filename="动态方法调用日志_{current_date}.xlsx"'
            excel_buffer.close()
            return response
        except Exception as e:
            logger.exception("导出方法调用日志失败")
            return Response({
                "code": 500,
                "msg": f"导出失败：{str(e)}"
            }, status=500)

class BatchRevokeDataPermissionView(APIView):
    """批量撤销数据权限API"""
    permission_classes = [IsAdminUser]

    def post(self, request):
        serializer = BatchRevokeDataPermissionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        q = Q()
        if data.get("user_ids"):
            q &= Q(user_id__in=data["user_ids"])
        if data.get("model_name"):
            q &= Q(model_name=data["model_name"])
        if data.get("data_ids"):
            q &= Q(data_id__in=data["data_ids"])

        if not q:
            return Response({
                "code": 400,
                "msg": "至少需要指定一个筛选条件（user_ids/model_name/data_ids）"
            }, status=400)

        deleted_count, _ = DataPermission.objects.filter(q).delete()
        return Response({
            "code": 200,
            "msg": f"成功撤销{deleted_count}条数据权限",
            "data": {
                "model_name": data.get("model_name"),
                "user_ids": data.get("user_ids", []),
                "data_ids": data.get("data_ids", []),
                "deleted_count": deleted_count
            }
        })

class AsyncExportMethodLogView(APIView):
    """异步导出方法调用日志API"""
    permission_classes = [IsAdminUser]

    def get(self, request):
        params = {k: v for k, v in {
            "user": request.query_params.get("user"),
            "model_name": request.query_params.get("model_name"),
            "result_status": request.query_params.get("result_status"),
            "start_time": request.query_params.get("start_time"),
            "end_time": request.query_params.get("end_time"),
        }.items() if v is not None}

        # 预检查记录数
        queryset = _apply_log_filters(LowCodeMethodCallLog.objects.all(), params)
        if queryset.count() > EXPORT_MAX_RECORDS * 5:  # 异步导出限制放宽5倍
            return Response({
                "code": 400,
                "msg": f"导出记录数超过最大限制 ({EXPORT_MAX_RECORDS * 5})",
                "data": {"max_records": EXPORT_MAX_RECORDS * 5, "current_count": queryset.count()}
            }, status=400)

        task = async_export_method_log.delay(params)
        return Response({
            "code": 200,
            "msg": "导出任务已启动，请查询进度",
            "data": {
                "task_id": task.id,
                "query_params": params,
                "estimated_count": queryset.count()
            }
        })

class ExportProgressView(APIView):
    """导出进度查询API"""
    permission_classes = [IsAdminUser]

    def get(self, request):
        task_id = request.query_params.get("task_id")
        if not task_id:
            return Response({"code": 400, "msg": "缺少task_id参数"}, status=400)

        progress = cache.get(f"export_progress_{task_id}")
        if progress is None:
            return Response({"code": 404, "msg": "任务不存在或已过期（有效期24小时）"}, status=404)

        data = {
            "task_id": task_id,
            "progress": progress,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        if progress == 100:
            file_path = cache.get(f"export_file_{task_id}")
            if file_path and default_storage.exists(file_path):
                data["download_url"] = request.build_absolute_uri(
                    f"/lowcode/api/v1/download-export/?file_path={file_path}")
                data["msg"] = "导出完成，可下载"
            else:
                data["msg"] = "导出完成，但文件不存在"
        elif progress == -1:
            error = cache.get(f"export_error_{task_id}", "未知错误")
            data["msg"] = f"导出失败：{error}"
        else:
            data["msg"] = f"导出中，进度{progress}%"

        return Response({"code": 200, "data": data})

class DownloadExportView(APIView):
    """下载导出文件API"""
    permission_classes = [IsAdminUser]

    def get(self, request):
        file_path = request.query_params.get("file_path")
        if not file_path:
            return Response({"code": 400, "msg": "缺少file_path参数"}, status=400)

        # 安全检查
        if not file_path.startswith(EXPORT_SUBDIR):
            return Response({"code": 400, "msg": "非法文件路径"}, status=400)

        try:
            filename = os.path.basename(file_path)
            _sanitize_filename(filename)

            if not default_storage.exists(file_path):
                return Response({"code": 404, "msg": "文件不存在或已过期"}, status=404)

            file = default_storage.open(file_path, 'rb')
            response = FileResponse(file,
                                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            response["Content-Disposition"] = f'attachment; filename="{filename}"'
            return response
        except ValidationError as e:
            return Response({"code": 400, "msg": str(e)}, status=400)
        except Exception as e:
            logger.exception(f"下载文件失败：{file_path}")
            return Response({"code": 500, "msg": f"下载失败：{str(e)}"}, status=500)

# ========== Misc Views ==========
def prometheus_metrics(request: HttpRequest):
    """Prometheus监控指标视图"""
    try:
        return HttpResponse(generate_latest(), content_type=CONTENT_TYPE_LATEST)
    except Exception as e:
        logger.exception("获取监控指标失败")
        return HttpResponse(status=500)

@staff_member_required
def create_lowcode_user_example(request: HttpRequest):
    """创建示例用户视图（仅用于演示）"""
    if not settings.DEBUG:
        return JsonResponse({"code": 403, "msg": "演示功能仅在开发环境可用"}, status=403)

    try:
        django_user, _ = User.objects.get_or_create(username="demo_user", defaults={"email": "demo@example.com"})
        role, _ = Role.objects.get_or_create(code="editor", defaults={"name": "编辑员"})
        lowcode_user, created = LowCodeUser.objects.update_or_create(
            user=django_user,
            defaults={
                "employee_id": "EMP1001",
                "department": "产品研发部",
                "phone": "13800138000",
                "role": role
            }
        )
        return JsonResponse({
            "code": 200,
            "status": "success" if created else "updated",
            "msg": "示例用户创建/更新成功",
            "data": {
                "username": lowcode_user.user.username,
                "email": lowcode_user.user.email,
                "employee_id": lowcode_user.employee_id,
                "department": lowcode_user.department,
                "phone": lowcode_user.phone,
                "role": lowcode_user.role.name if lowcode_user.role else None
            }
        })
    except Exception as e:
        logger.exception("创建示例用户失败")
        return JsonResponse({"code": 500, "msg": f"创建失败：{str(e)}"}, status=500)

@staff_member_required
def get_lowcode_user_detail(request: HttpRequest, user_id: int):
    """获取低代码用户详情视图（限制管理员权限）"""
    try:
        lowcode_user = get_object_or_404(LowCodeUser, user_id=user_id)
        return JsonResponse({
            "code": 200,
            "data": {
                "user_id": lowcode_user.user_id,
                "username": lowcode_user.user.username,
                "email": lowcode_user.user.email,
                "employee_id": lowcode_user.employee_id,
                "department": lowcode_user.department,
                "phone": lowcode_user.phone,
                "role": lowcode_user.role.name if lowcode_user.role else None,
                "avatar_url": lowcode_user.avatar.url if lowcode_user.avatar else None,
                "date_joined": lowcode_user.user.date_joined.strftime("%Y-%m-%d %H:%M:%S"),
                "last_login": lowcode_user.user.last_login.strftime(
                    "%Y-%m-%d %H:%M:%S") if lowcode_user.user.last_login else None
            }
        })
    except Exception as e:
        logger.exception(f"获取用户详情失败：user_id={user_id}")
        return JsonResponse({"code": 500, "msg": f"获取失败：{str(e)}"}, status=500)

# ========== API根路径视图（移至views.py便于复用） ==========
class APIRootView(APIView):
    """低代码平台API接口根文档"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            "message": "低代码平台API接口文档（v1）",
            "version": "1.0.0",
            "description": "提供动态模型数据管理、权限控制、日志导出等功能",
            "endpoints": {
                "方法调用日志": request.build_absolute_uri("method-call-logs/"),
                "批量数据权限": request.build_absolute_uri("batch-data-permission/"),
                "日志导出": request.build_absolute_uri("export-method-logs/"),
                "异步日志导出": request.build_absolute_uri("async-export-method-logs/")
            },
            "notice": "生产环境建议限制API访问IP，避免敏感操作泄露"
        })