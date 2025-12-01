# lowcode/tasks.py
import logging
import os
import uuid
from io import BytesIO
from typing import Any, Dict, List
import json
import traceback

from django.conf import settings
from django.core.cache import cache
from django.core.files.storage import default_storage
from django.db import transaction
from django.contrib.auth.models import User
from django.core.management import call_command
from django.apps import apps
from celery import shared_task

from .models import LowCodeMethodCallLog, ModelLowCode, ModelUpgradeRecord
from lowcode.io.excel import generate_method_log_excel
from .models.dynamic_model_factory import get_dynamic_model, refresh_dynamic_model
from .core.ddl_executor import create_table_if_not_exists

logger = logging.getLogger(__name__)

# === 配置项 ===
EXPORT_SUBDIR = getattr(settings, 'LOWCODE_EXPORT_SUBDIR', 'lowcode_exports/')
EXPORT_MAX_RECORDS = getattr(settings, 'LOWCODE_EXPORT_MAX_RECORDS', 100_000)
EXPORT_CACHE_TIMEOUT = getattr(settings, 'LOWCODE_EXPORT_CACHE_TIMEOUT', 3600)  # 1小时


def _get_export_storage_path(filename: str) -> str:
    """返回相对于 MEDIA 的存储路径"""
    return os.path.join(EXPORT_SUBDIR.strip('/'), filename)


# ────────────────────────────────────────
# 任务 1：异步导出方法调用日志（Excel）
# ────────────────────────────────────────

@shared_task(bind=True, max_retries=2)
def async_export_method_log(self, filter_params: dict):
    """
    异步导出方法调用日志为 Excel 文件。
    """
    task_id = self.request.id
    progress_key = f"export_progress_{task_id}"
    file_key = f"export_file_{task_id}"
    error_key = f"export_error_{task_id}"

    def _set_progress(percent: int):
        cache.set(progress_key, percent, timeout=EXPORT_CACHE_TIMEOUT)

    def _set_error(msg: str):
        cache.set(progress_key, -1, timeout=EXPORT_CACHE_TIMEOUT)
        cache.set(error_key, msg, timeout=EXPORT_CACHE_TIMEOUT)
        logger.error(f"[ERROR] [Export Task {task_id}] Error: {msg}")

    try:
        logger.info(f"[OK] [Export Task {task_id}] Started with filters: {filter_params}")
        _set_progress(0)

        queryset = LowCodeMethodCallLog.objects.all()
        if filter_params.get("user"):
            queryset = queryset.filter(user_id=filter_params["user"])
        if filter_params.get("model_name"):
            queryset = queryset.filter(model_name__icontains=filter_params["model_name"])
        if filter_params.get("result_status"):
            queryset = queryset.filter(result_status=filter_params["result_status"])
        if filter_params.get("start_time"):
            queryset = queryset.filter(call_time__gte=filter_params["start_time"])
        if filter_params.get("end_time"):
            queryset = queryset.filter(call_time__lte=filter_params["end_time"])

        total_count = queryset.count()
        if total_count == 0:
            _set_progress(100)
            cache.set(file_key, None, timeout=EXPORT_CACHE_TIMEOUT)
            logger.info(f"[OK] [Export Task {task_id}] No records to export.")
            return None

        if total_count > EXPORT_MAX_RECORDS:
            error_msg = f"导出记录数 ({total_count}) 超过最大限制 ({EXPORT_MAX_RECORDS})"
            _set_error(error_msg)
            raise ValueError(error_msg)

        _set_progress(20)
        logger.info(f"[OK] [Export Task {task_id}] Found {total_count} records to export.")

        excel_buffer: BytesIO = generate_method_log_excel(queryset)
        _set_progress(80)

        filename = f"method_log_{uuid.uuid4().hex}.xlsx"
        storage_path = _get_export_storage_path(filename)

        with default_storage.open(storage_path, 'wb') as f:
            f.write(excel_buffer.getvalue())
        excel_buffer.close()

        _set_progress(100)
        cache.set(file_key, storage_path, timeout=EXPORT_CACHE_TIMEOUT)

        logger.info(f"[OK] [Export Task {task_id}] Export completed. File: {storage_path}")
        return storage_path

    except Exception as exc:
        error_msg = str(exc)
        _set_error(error_msg)
        logger.exception(f"[EXCEPTION] [Export Task {task_id}] Failed with exception:")
        raise self.retry(exc=exc, countdown=60)


# ────────────────────────────────────────
# 任务 2：异步刷新并创建动态模型表
# ────────────────────────────────────────

@shared_task(bind=True, max_retries=3)
def async_refresh_and_create_table(self, model_config_id: int):
    """
    异步任务：根据 ModelLowCode 配置创建/更新数据库表，并刷新动态模型缓存。
    """
    try:
        logger.info(f"[OK] [Refresh Table Task] Starting for model_config_id={model_config_id}")
        model_config = ModelLowCode.objects.get(id=model_config_id)

        dynamic_model_class = get_dynamic_model(model_config)
        create_table_if_not_exists(dynamic_model_class)
        refresh_dynamic_model(model_config.name)

        logger.info(f"[OK] 成功刷新模型 {model_config.name} (ID={model_config_id})")
        return f"Success: {model_config.name}"

    except ModelLowCode.DoesNotExist:
        msg = f"[WARNING] 模型配置 ID={model_config_id} 不存在"
        logger.warning(msg)
        return msg
    except Exception as exc:
        logger.exception(f"[EXCEPTION] 异步刷新失败 (model_config_id={model_config_id}): {exc}")
        raise self.retry(exc=exc, countdown=60)


# ────────────────────────────────────────
# 任务 3：异步执行动态模型升级（带状态持久化）
# ────────────────────────────────────────

@shared_task(bind=True, max_retries=1, autoretry_for=(Exception,), retry_kwargs={'countdown': 5})
def async_upgrade_model_task(
        self,
        model_name: str,
        fields: List[Dict[str, Any]],
        user_id: int,
        task_id: str,
        **options
) -> Dict[str, str]:
    """
    异步执行动态模型升级命令，并更新 ModelUpgradeRecord 状态。
    """
    logger.info(f"[OK] 开始执行模型升级任务: task_id={task_id}, model={model_name}, user_id={user_id}")

    user = User.objects.filter(id=user_id).first()
    username = user.username if user else f"user#{user_id}"

    try:
        # 更新状态为 running
        with transaction.atomic():
            record = ModelUpgradeRecord.objects.select_for_update().get(task_id=task_id)
            record.status = 'running'
            record.save(update_fields=['status'])

        # 执行管理命令
        fields_json = json.dumps(fields, ensure_ascii=False)
        call_command(
            'upgrade_model',
            model_name,
            fields=fields_json,
            no_backup=options.get('no_backup', False),
            no_restart=options.get('no_restart', False),
            force=options.get('force', False),
            verbosity=0
        )

        # 标记成功
        with transaction.atomic():
            record = ModelUpgradeRecord.objects.select_for_update().get(task_id=task_id)
            record.status = 'success'
            record.save(update_fields=['status'])

        logger.info(f"[OK] 模型 {model_name} 升级成功，task_id={task_id}，操作人：{username}")
        return {"status": "success", "task_id": task_id}

    except Exception as exc:
        error_msg = str(exc)[:500]
        logger.error(f"[ERROR] 模型升级失败 task_id={task_id}: {error_msg}", exc_info=True)

        try:
            with transaction.atomic():
                record = ModelUpgradeRecord.objects.select_for_update().get(task_id=task_id)
                record.status = 'failed'
                record.error_message = error_msg
                record.save(update_fields=['status', 'error_message'])
        except ModelUpgradeRecord.DoesNotExist:
            logger.warning(f"[WARNING] 任务记录不存在，无法更新失败状态: task_id={task_id}")

        raise self.retry(exc=exc, countdown=5)


# ────────────────────────────────────────
# 辅助函数（供视图调用）
# ────────────────────────────────────────

def start_upgrade_task(
        model_name: str,
        fields: List[Dict[str, Any]],
        user_id: int,
        **options
) -> str:
    """启动异步升级任务，并创建初始记录。"""
    task_id = str(uuid.uuid4())
    ModelUpgradeRecord.objects.create(
        task_id=task_id,
        model_name=model_name,
        fields=fields,
        created_by_id=user_id,
        status='pending',
        no_backup=options.get('no_backup', False),
        no_restart=options.get('no_restart', False),
        force=options.get('force', False),
    )
    async_upgrade_model_task.delay(model_name, fields, user_id, task_id, **options)
    return task_id


def get_task_status(task_id: str) -> dict:
    """查询任务状态（供 API 使用）"""
    try:
        record = ModelUpgradeRecord.objects.get(task_id=task_id)
        return {
            "task_id": record.task_id,
            "model_name": record.model_name,
            "status": record.status,
            "error_message": record.error_message,
            "created_at": record.created_at.isoformat(),
            "created_by": record.created_by.username if record.created_by else None,
        }
    except ModelUpgradeRecord.DoesNotExist:
        return {"status": "not_found", "task_id": task_id}


# ────────────────────────────────────────
# 任务 4：异步保存方法调用日志（供 record_method_call_log 装饰器使用）
# ────────────────────────────────────────

@shared_task(
    bind=True,
    name="lowcode.log.save_method_call",
    max_retries=3,
    default_retry_delay=5
)
def save_method_call_log_task(self, log_data):
    """
    异步保存方法调用日志。
    接收来自 utils/log.py 装饰器的日志数据。
    """
    if not isinstance(log_data, dict):
        logger.error(f"Invalid log_data type: {type(log_data)}. Expected dict.")
        return

    try:
        # 使用项目中实际的日志模型
        LogModel = apps.get_model('lowcode', 'LowCodeMethodCallLog')

        # 防止 result_data 或 exception_msg 超出数据库字段长度（如 MySQL TEXT 最大 65535 字节）
        for field in ("result_data", "exception_msg"):
            value = log_data.get(field)
            if value and len(str(value)) > 65535:
                log_data[field] = str(value)[:65532] + "..."

        with transaction.atomic():
            LogModel.objects.create(**log_data)

        logger.debug(
            f"Async method log saved: {log_data.get('method_name')} "
            f"on model {log_data.get('model_name')} by user {log_data.get('user')}"
        )

    except Exception as exc:
        error_msg = f"Failed to save async method call log: {exc}\n{traceback.format_exc()}"
        logger.error(error_msg)
        raise self.retry(exc=exc, countdown=2 ** self.request.retries)