# utils/log.py
import time
import json
from functools import wraps
from django.conf import settings
from django.db import transaction
from django.apps import apps
import logging

logger = logging.getLogger(__name__)

# 默认敏感字段（可扩展）
DEFAULT_SENSITIVE_KEYS = {'password', 'token', 'secret', 'auth', 'key', 'credential', 'pin', 'ssn'}


def _sanitize_value(value):
    """递归脱敏数据结构中的敏感字段"""
    if isinstance(value, dict):
        return {
            k: "[REDACTED]" if k.lower() in DEFAULT_SENSITIVE_KEYS else _sanitize_value(v)
            for k, v in value.items()
        }
    elif isinstance(value, (list, tuple)):
        return type(value)(_sanitize_value(item) for item in value)
    else:
        return value


def _get_user_identifier(user):
    """安全提取用户标识"""
    if hasattr(user, 'id'):
        return user.id
    elif hasattr(user, 'username'):
        return user.username
    else:
        return str(user)


def record_method_call_log(
    log_success: bool = True,
    log_failure: bool = True,
    include_result: bool = True,
    async_log: bool = None,  # None 表示使用全局配置
    exclude_params: list = None
):
    """
    增强型方法调用日志装饰器

    :param log_success: 是否记录成功调用
    :param log_failure: 是否记录失败调用
    :param include_result: 是否记录返回结果
    :param async_log: 是否异步写入日志（None 则读取 settings.ASYNC_METHOD_LOG）
    :param exclude_params: 需要从 kwargs 中排除的参数名列表（额外脱敏）
    """
    exclude_params = set(exclude_params or [])

    def decorator(func):
        @wraps(func)
        def wrapper(self, user, *args, **kwargs):
            # 全局开关
            if not getattr(settings, "ENABLE_METHOD_CALL_LOG", True):
                return func(self, user, *args, **kwargs)

            # 决定是否异步
            use_async = async_log if async_log is not None else getattr(settings, "ASYNC_METHOD_LOG", False)

            start_time = time.time()
            user_id = _get_user_identifier(user)

            # 脱敏参数
            sanitized_kwargs = _sanitize_value(kwargs)
            if exclude_params:
                for key in exclude_params:
                    sanitized_kwargs.pop(key, None)

            log_data = {
                "user": user_id,
                "model_name": self.__class__.__name__,
                "method_name": func.__name__,
                "params": {
                    "args": list(args),
                    "kwargs": sanitized_kwargs
                },
                "result_status": "success",
                "result_data": None,
                "exception_msg": None,
                "time_cost": 0
            }

            try:
                result = func(self, user, *args, **kwargs)
                if log_success and include_result:
                    try:
                        log_data["result_data"] = json.dumps(result, default=str, ensure_ascii=False)
                    except Exception as je:
                        log_data["result_data"] = f"[SERIALIZE ERROR: {str(je)}]"
                return result
            except Exception as e:
                if log_failure:
                    log_data["result_status"] = "fail"
                    log_data["exception_msg"] = str(e)
                raise
            finally:
                # 只有在需要记录的情况下才保存日志
                should_log = (
                    (log_data["result_status"] == "success" and log_success) or
                    (log_data["result_status"] == "fail" and log_failure)
                )
                if not should_log:
                    return

                log_data["time_cost"] = round(time.time() - start_time, 6)

                # 日志写入逻辑
                if use_async:
                    try:
                        from .tasks import save_method_call_log_task
                        save_method_call_log_task.delay(log_data)
                    except ImportError:
                        logger.warning("Async logging enabled but tasks.save_method_call_log_task not found. Falling back to sync.")
                        _save_log_sync(log_data)
                    except Exception as ae:
                        logger.error(f"Failed to enqueue async log: {ae}", exc_info=True)
                else:
                    _save_log_sync(log_data)

        return wrapper
    return decorator


def _save_log_sync(log_data):
    """同步保存日志到数据库"""
    try:
        MethodLowCode = apps.get_model('lowcode', 'MethodLowCode')  # ⚠️
        with transaction.atomic():
            MethodLowCode.objects.create(**log_data)
    except Exception as e:
        logger.error(f"Failed to save method call log synchronously: {e}", exc_info=True)