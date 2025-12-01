# lowcode/api/views.py
"""
低代码平台 API 视图模块。

提供两大核心能力：
1. 动态数据库表创建（即时 DDL）
2. 动态模型字段升级（异步任务）

接口列表：
- GET  /api/lowcode/                    → 健康检查
- POST /api/lowcode/create-table/       → 创建表（同步）
- POST /api/lowcode/upgrade-model/      → 启动升级任务（异步）
- GET  /api/lowcode/upgrade-status/<task_id>/ → 查询任务状态
- GET  /api/lowcode/upgrade-history/    → 获取升级历史
"""
# curl -X POST http://localhost:8000/api/v1/lowcode/create-table/ \
#   -H "Authorization: Token YOUR_ADMIN_TOKEN" \
#   -H "Content-Type: application/json" \
#   -d '{
#     "table_name": "user_events_2025",
#     "sample_data": {
#       "event_id": "evt_123",
#       "user_id": 1001,
#       "event_type": "click",
#       "metadata": "{\"page\": \"/home\"}"
#     },
#     "primary_key": ["event_id"],
#     "indexes": [["user_id"], ["event_type", "user_id"]],
#     "database_alias": "default"
#   }'

# 响应示例
# 成功（新表创建）
#
# Json
# 编辑
# {
#   "success": true,
#   "message": "表 'user_events_2025' 已准备就绪",
#   "created": true,
#   "table_name": "user_events_2025",
#   "database": "default"
# }
# 成功（表已存在）
#
# Json
# 编辑
# {
#   "success": true,
#   "message": "表 'user_events_2025' 已准备就绪",
#   "created": false,
#   "table_name": "user_events_2025",
#   "database": "default"
# }
# 错误（参数无效）
#
# Json
# 编辑
# {
#   "error": "参数校验失败",
#   "details": {
#     "table_name": ["表名只能包含字母、数字、下划线或连字符"]
#   }
# }
import logging
from typing import Any, Dict, List

from django.db import connections
from rest_framework.decorators import api_view
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from rest_framework.permissions import IsAdminUser

from .serializers import DynamicTableCreateSerializer, UpgradeModelSerializer
from lowcode.models.models import ModelUpgradeRecord
from lowcode.utils.table_manager import ensure_table_exists
from Icent_LowCode.version import __version__

# 日志
logger = logging.getLogger(__name__)


# ========================
# 健康检查 & 元信息
# ========================

@api_view(['GET'])
def api_root(request):
    return Response({
        'name': 'Icent LowCode Platform',
        'version': __version__,
        'status': 'ok'
    })


# ========================
# 异步任务后端自动适配
# ========================

try:
    from lowcode.tasks_celery import async_upgrade_model_task, get_task_status
    TASK_BACKEND = 'celery'
except ImportError:
    from lowcode.tasks_threading import async_upgrade_model_task, get_task_status
    TASK_BACKEND = 'thread'


# ========================
# 接口 1：动态创建数据库表（同步）
# ========================

class DynamicTableCreateView(APIView):
    """
    动态创建数据库表（低代码平台专用）

    POST /api/lowcode/create-table/
    权限：仅限管理员
    """
    permission_classes = [IsAdminUser]

    def post(self, request, *args, **kwargs):
        serializer = DynamicTableCreateSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(
                {"error": "参数校验失败", "details": serializer.errors},
                status=status.HTTP_400_BAD_REQUEST
            )

        data: Dict[str, Any] = serializer.validated_data
        table_name = data["table_name"]
        sample_data = data["sample_data"]
        primary_key = data.get("primary_key") or None
        indexes = data.get("indexes", [])
        database_alias = data["database_alias"]

        try:
            conn = connections[database_alias]
            created = ensure_table_exists(
                conn,
                table_name=table_name,
                sample_data=sample_data,
                primary_key=primary_key,
                indexes=indexes
            )
            username = getattr(request.user, 'username', 'anonymous')
            user_id = getattr(request.user, 'id', None)
            action = "created" if created else "already exists"
            logger.info(
                f"Dynamic table '{table_name}' {action} on '{database_alias}' "
                f"by user {username} ({user_id})"
            )

            return Response({
                "success": True,
                "message": f"表 '{table_name}' 已准备就绪",
                "created": created,
                "table_name": table_name,
                "database": database_alias
            }, status=status.HTTP_201_CREATED if created else status.HTTP_200_OK)

        except Exception as e:
            logger.error(
                f"动态建表失败 (table={table_name}, db={database_alias}): {e}",
                exc_info=True
            )
            return Response({
                "error": "动态建表失败",
                "details": str(e)
            }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ========================
# 接口 2：启动模型升级任务（异步）
# ========================

class UpgradeModelAPIView(APIView):
    """
    启动动态模型字段升级任务（异步执行）。
    """
    permission_classes = [IsAdminUser]

    def post(self, request):
        serializer = UpgradeModelSerializer(data=request.data, context={'request': request})
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            user_id = request.user.id if request.user.is_authenticated else None
            username = getattr(request.user, 'username', 'anonymous')

            # 启动异步任务
            if TASK_BACKEND == 'celery':
                task = async_upgrade_model_task.delay(
                    model_name=data['model_name'],
                    fields=data['fields'],
                    user_id=user_id,
                    no_backup=data['no_backup'],
                    no_restart=data['no_restart'],
                    force=data['force']
                )
                task_id = task.id
            else:  # thread
                task_id = async_upgrade_model_task(
                    model_name=data['model_name'],
                    fields=data['fields'],
                    user_id=user_id,
                    no_backup=data['no_backup'],
                    no_restart=data['no_restart'],
                    force=data['force']
                )

            logger.info(
                f"Upgrade task started: model={data['model_name']}, "
                f"task_id={task_id}, backend={TASK_BACKEND}, user={username} ({user_id})"
            )

            return Response({
                "task_id": task_id,
                "message": "升级任务已启动",
                "status_check_url": f"/api/lowcode/upgrade-status/{task_id}/"
            }, status=status.HTTP_202_ACCEPTED)

        except Exception as e:
            logger.error(f"启动升级任务失败: {e}", exc_info=True)
            return Response(
                {"error": f"任务启动失败: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


# ========================
# 接口 3：查询任务状态
# ========================

class UpgradeStatusAPIView(APIView):
    """
    查询指定升级任务的执行状态。
    """
    permission_classes = [IsAdminUser]

    def get(self, request, task_id: str):
        if not isinstance(task_id, str) or not task_id.strip():
            return Response({"error": "无效的 task_id"}, status=status.HTTP_400_BAD_REQUEST)

        clean_task_id = task_id.strip()
        try:
            status_info = get_task_status(clean_task_id)
            if not status_info:
                return Response({"error": "任务不存在或已过期"}, status=status.HTTP_404_NOT_FOUND)
            return Response(status_info)
        except Exception as e:
            logger.error(f"查询任务状态失败 (task_id={clean_task_id}): {e}", exc_info=True)
            return Response(
                {"error": f"查询状态失败: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )


# ========================
# 接口 4：获取升级历史
# ========================

class UpgradeHistoryAPIView(APIView):
    """
    获取最近的模型升级历史记录（最多 20 条）。
    """
    permission_classes = [IsAdminUser]

    def get(self, request):
        records = (
            ModelUpgradeRecord.objects
            .select_related('created_by')
            .order_by('-created_at')[:20]
        )

        history: List[Dict[str, Any]] = []
        for record in records:
            history.append({
                "id": record.id,
                "model_name": record.model_name,
                "status": record.status,
                "created_at": record.created_at.isoformat(),
                "error_message": record.error_message or "",
                "created_by_username": getattr(record.created_by, 'username', None),
                "fields": record.fields or [],
            })

        return Response(history)