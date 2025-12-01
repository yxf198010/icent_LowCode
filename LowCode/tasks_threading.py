# lowcode/api/tasks_celery.py
"""
线程版异步模型升级任务（ 仅适用于开发或单机部署！）
进程重启后任务状态会丢失，生产环境请使用 Celery + DB 持久化方案。
"""

import threading
import logging
import json
import uuid
from typing import Any, Dict, List

from django.core.management import call_command
from django.core.management.base import CommandError
from django.contrib.auth.models import User
from lowcode.models.models import ModelUpgradeRecord

logger = logging.getLogger(__name__)

# 内存任务状态（非持久化，仅用于快速查询）
_TASK_STATUS: Dict[str, Dict[str, Any]] = {}


def _run_upgrade(model_name: str, fields: List[Dict[str, Any]], task_id: str, user_id: int, **options) -> None:
    """
    在子线程中执行模型升级命令，并同步更新 DB 记录与内存状态。
    """
    try:
        # 更新数据库记录为 running
        record = ModelUpgradeRecord.objects.get(task_id=task_id)
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

        # 升级成功
        record.status = 'success'
        record.save(update_fields=['status'])
        _TASK_STATUS[task_id] = {
            "status": "success",
            "message": f"模型 {model_name} 升级成功"
        }
        logger.info(f"[OK] 模型升级成功: task_id={task_id}, model={model_name}, user_id={user_id}")

    except CommandError as e:
        # 管理命令抛出的业务错误
        error_msg = str(e)[:500]
        _handle_failure(task_id, error_msg, model_name)
    except Exception as e:
        # 其他系统异常
        error_msg = f"内部错误: {str(e)}"[:500]
        _handle_failure(task_id, error_msg, model_name)
        logger.error(f"[ERROR] 模型升级异常 task_id={task_id}: {e}", exc_info=True)


def _handle_failure(task_id: str, error_msg: str, model_name: str) -> None:
    """统一处理失败逻辑"""
    try:
        record = ModelUpgradeRecord.objects.get(task_id=task_id)
        record.status = 'failed'
        record.error_message = error_msg
        record.save(update_fields=['status', 'error_message'])
    except ModelUpgradeRecord.DoesNotExist:
        logger.warning(f"[WARNING] 任务记录不存在，无法更新失败状态: task_id={task_id}")

    _TASK_STATUS[task_id] = {
        "status": "failed",
        "error": error_msg,
        "model_name": model_name
    }


def async_upgrade_model_task(
    model_name: str,
    fields: List[Dict[str, Any]],
    user_id: int,
    **options
) -> str:
    """
    启动一个后台线程执行模型升级，并返回任务 ID。

    ⚠️ 注意：此实现不支持进程重启后的状态恢复，仅推荐用于开发环境！

    :param model_name: 动态模型名称
    :param fields: 字段定义列表（已校验）
    :param user_id: 操作用户 ID
    :param options: 其他选项（no_backup, no_restart, force）
    :return: 任务唯一 ID（可用于轮询状态）
    """
    task_id = str(uuid.uuid4())

    # 创建初始记录（pending）
    ModelUpgradeRecord.objects.create(
        model_name=model_name,
        fields=fields,
        task_id=task_id,
        created_by_id=user_id,
        status='pending',
        no_backup=options.get('no_backup', False),
        no_restart=options.get('no_restart', False),
        force=options.get('force', False),
    )

    # 初始化内存状态
    _TASK_STATUS[task_id] = {"status": "pending"}

    # 启动后台线程
    thread = threading.Thread(
        target=_run_upgrade,
        args=(model_name, fields, task_id, user_id),
        kwargs=options,
        daemon=True  # 主线程退出时自动销毁
    )
    thread.start()

    logger.debug(f"[DEBUG] 已启动模型升级任务: task_id={task_id}, model={model_name}")
    return task_id


def get_task_status(task_id: str) -> Dict[str, Any]:
    """
    获取任务状态（从内存字典读取）。

     进程重启后所有状态丢失！生产环境应查询 ModelUpgradeRecord 数据库表。
    """
    return _TASK_STATUS.get(task_id, {"status": "not_found", "task_id": task_id})