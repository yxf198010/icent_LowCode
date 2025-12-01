# lowcode/decorators/audit_log.py
"""
低代码平台方法调用审计日志装饰器

用于记录用户对动态模型方法的调用行为，包括：
- 调用者（user）
- 方法所属模型/服务名
- 方法名
- 输入参数（args/kwargs）
- 返回结果（安全序列化）
- 异常信息（如有）
- 执行耗时

注意：
- 日志写入在独立数据库事务中进行，但不保证 100% 可靠（如主事务已崩溃）
- 生产环境建议配合异步任务（如 Celery）提升可靠性
- 默认关闭，需在 settings 中显式启用
"""
# # lowcode/services/order_service.py
# from lowcode.decorators.audit_log import record_method_call_log
#
# class OrderService:
#     @record_method_call_log()
#     def create_dynamic_order(self, user, table_name: str, data: dict):
#         # 1. 确保表存在
#         # 2. 插入数据
#         # 3. 返回订单ID
#         return {"order_id": 12345}
import time
import json
import logging
from functools import wraps
from typing import Any, Dict
from django.conf import settings
from django.db import transaction, connection

logger = logging.getLogger(__name__)

# 全局开关：默认关闭，避免性能开销
ENABLED = getattr(settings, "LOWCODE_METHOD_LOGGING_ENABLED", False)


def _safe_json_serialize(obj: Any) -> str:
    """安全地将任意对象转为 JSON 字符串，失败时返回错误占位符"""
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception as e:
        return f"[SERIALIZATION FAILED: {type(e).__name__}: {str(e)[:200]}]"


def _save_audit_log(log_data: Dict[str, Any]) -> None:
    """实际保存日志到数据库（在独立事务中）"""
    try:
        # 延迟导入，避免循环依赖
        from ..models import MethodLowCode

        # 使用独立事务（但无法完全隔离于外层崩溃）
        with transaction.atomic():
            MethodLowCode.objects.create(**log_data)
    except Exception as e:
        # 永远不要让日志失败影响主业务
        logger.warning(f"Failed to save audit log: {e}", exc_info=True)


def record_method_call_log():
    """
    装饰器：记录低代码方法调用日志

    要求被装饰方法签名为：
        def method(self, user, *args, **kwargs) -> Any

    其中：
        - self: 通常为服务类或模型管理器实例
        - user: 当前操作用户（支持 str / int / User 对象）

    示例：
        class OrderService:
            @record_method_call_log()
            def create_order(self, user, order_data):
                ...
    """
    if not ENABLED:
        # 若未启用，直接返回原函数（零开销）
        def noop_decorator(func):
            return func
        return noop_decorator

    def decorator(func):
        @wraps(func)
        def wrapper(self, user, *args, **kwargs):
            start_time = time.time()
            log_data = {
                "user": str(user) if user is not None else "anonymous",
                "model_name": self.__class__.__name__,
                "method_name": func.__name__,
                "params": {
                    "args": list(args),
                    "kwargs": kwargs,
                },
                "result_status": "success",
                "result_data": None,
                "exception_msg": None,
                "time_cost": 0.0,
            }

            result = None
            try:
                result = func(self, user, *args, **kwargs)
                log_data["result_data"] = _safe_json_serialize(result)
                return result
            except Exception as e:
                log_data["result_status"] = "fail"
                log_data["exception_msg"] = str(e)
                raise  # 重新抛出，不干扰业务异常流
            finally:
                log_data["time_cost"] = round(time.time() - start_time, 6)
                # 异步保存是更优解，此处为简化仍同步写入
                # TODO: 替换为 enqueue(save_audit_log_task, log_data)
                _save_audit_log(log_data)

        return wrapper
    return decorator