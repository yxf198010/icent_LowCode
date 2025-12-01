# lowcode/signals.py
"""
低代码平台信号处理器。

功能：
1. 开发环境：监听 ModelLowCode 变更，自动 makemigrations + migrate；
2. 生产环境：发送异步任务 async_refresh_and_create_table 创建/更新表；
3. 删除模型时同样处理；
4. 为 staff 用户自动创建 DRF Token；
5. 避免重复触发、并发执行、非关键字段变更误触发。
"""

import logging
import time
import threading
from typing import Set, Tuple

from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.core.management import call_command
from django.conf import settings
from django.contrib.auth.models import User
from rest_framework.authtoken.models import Token

from .models import ModelLowCode
from .models.dynamic_model_factory import refresh_dynamic_methods
from .tasks import async_refresh_and_create_table

logger = logging.getLogger(__name__)

# 全局锁：防止并发执行迁移命令
_MIGRATION_LOCK = threading.Lock()

# 去重缓存：{(model_id, timestamp)}
_DEDUPE_CACHE: Set[Tuple[int, float]] = set()
_DEDUPE_LOCK = threading.Lock()
_DEDUPE_WINDOW_SECONDS = 10  # 10 秒内相同 model_id 不重复触发


def _is_auto_migrate_enabled() -> bool:
    """
    判断是否启用 Django 自动迁移（仅适用于开发环境）。
    优先级：
      1. settings.LOWCODE_AUTO_MIGRATE（显式控制）
      2. 否则：仅当 DEBUG=True 时启用（安全默认）
    """
    if hasattr(settings, 'LOWCODE_AUTO_MIGRATE'):
        return bool(settings.LOWCODE_AUTO_MIGRATE)
    return getattr(settings, 'DEBUG', False)


def _should_skip_trigger(model_id: int) -> bool:
    """基于时间窗口的去重：避免短时间内重复处理同一模型"""
    now = time.time()
    cutoff = now - _DEDUPE_WINDOW_SECONDS

    with _DEDUPE_LOCK:
        # 清理过期条目
        _DEDUPE_CACHE.difference_update(
            {(mid, ts) for mid, ts in _DEDUPE_CACHE if ts < cutoff}
        )
        # 检查是否近期已触发
        if any(mid == model_id for mid, _ in _DEDUPE_CACHE):
            return True
        # 记录本次触发
        _DEDUPE_CACHE.add((model_id, now))
        return False


def _has_structure_changed(instance) -> bool:
    """
    判断 fields / table_name / name 是否真正变更。
    避免因 update_time 等无关字段更新而误触发。
    """
    if not instance.pk:
        return True  # 新建模型总是需要处理

    try:
        old = ModelLowCode.objects.get(pk=instance.pk)
        return (
            old.fields != instance.fields
            or old.table_name != instance.table_name
            or old.name != instance.name
        )
    except ModelLowCode.DoesNotExist:
        return True
