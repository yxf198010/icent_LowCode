# lowcode/services/async_multi_table_transaction_service.py
# 支持 Django 异步 ORM（aget, acreate, abulk_create 等）
# 提供 @async_universal_transaction 装饰器
# 支持超时（秒级）、重试次数、重试延迟
# 仅对特定异常（如并发冲突）重试
# 自动耗时统计与日志
# 保留原有 MultiTableTransactionService 的核心逻辑，但新增 异步版本方法
# 兼容动态模型（app_label, model_name 字符串）
# ⚠️ 要求：Django ≥ 3.2，且数据库后端支持异步（如 PostgreSQL + psycopg）
# views.py 或 tasks.py

# from lowcode.services.async_multi_table_transaction_service import (
#     AsyncMultiTableTransactionService,
#     async_universal_transaction
# )
#
# @async_universal_transaction(
#     model_names=["SalesOrder", "SalesOrderItem"],
#     timeout=5.0,
#     retry_times=3,
#     retry_delay=0.5
# )
# async def create_sales_order_async(master_data: dict, detail_list: list):
#     return await AsyncMultiTableTransactionService.create_master_with_details(
#         master_model_name="SalesOrder",
#         detail_model_name="SalesOrderItem",
#         master_data=master_data,
#         detail_list=detail_list,
#         foreign_key_field="order",
#         validate_amount_consistency=True
#     )
#
#
# # 在 async view 或 Celery task 中调用
# async def my_async_view(request):
#     master = {"order_no": "SO20251122002", "amount": 199.99, "status": 1}
#     details = [{"product_name": "键盘", "price": 199.99, "quantity": 1}]
#     order = await create_sales_order_async(master, details)
#     return JsonResponse({"order_id": order.pk})

# ✅ 注意事项
# 项目	说明
# Django 版本	≥ 3.2（推荐 ≥ 4.2 以支持 abulk_create）
# 数据库	推荐 PostgreSQL（MySQL 异步支持有限）
# 事务原子性	依赖 transaction.atomic()，在异步函数中仍有效
# 超时控制	使用 asyncio.wait_for，可中断 Python 层，但无法强制终止 DB 查询
# 生产建议	结合数据库锁超时（如 innodb_lock_wait_timeout=3s）
# 此实现既保持了与你现有低代码架构的兼容性，又提供了企业级异步事务能力，可直接用于订单、支付、库存等核心业务场景。
import asyncio
import functools
import time
import logging
from typing import Dict, List, Optional, Any, Union, Tuple, Callable
from decimal import Decimal

from django.apps import apps
from django.db import transaction
from django.core.exceptions import ObjectDoesNotExist

logger = logging.getLogger(__name__)


class AsyncMultiTableTransactionService:
    """
    异步通用多表事务服务类（动态模型 + 主从结构）
    """

    @staticmethod
    @transaction.atomic
    async def create_master_with_details(
        master_model_name: str,
        detail_model_name: str,
        master_data: Dict[str, Any],
        detail_list: List[Dict[str, Any]],
        master_app_label: str = "lowcode",
        detail_app_label: str = "lowcode",
        foreign_key_field: str = "master",
        amount_field_in_master: Optional[str] = "amount",
        price_field_in_detail: Optional[str] = "price",
        quantity_field_in_detail: Optional[str] = "quantity",
        validate_amount_consistency: bool = True,
    ):
        """
        异步创建主表 + 子表明细（在 atomic 事务内执行）
        注意：Django 的 transaction.atomic 不是 async-native，
        但可在 async 函数中使用（需运行在 sync_to_async 或 ASGI 环境下）。
        实际 ORM 操作必须使用 async 方法（acreate 等）。
        """
        # 1. 获取动态模型类
        try:
            MasterModel = apps.get_model(master_app_label, master_model_name)
            DetailModel = apps.get_model(detail_app_label, detail_model_name)
        except LookupError as e:
            raise ValueError(f"模型未注册或不存在: {e}")

        # 2. 异步创建主表记录
        master_obj = await MasterModel.objects.acreate(**master_data)
        logger.debug(f"异步创建主表记录: {master_model_name} ID={master_obj.pk}")

        # 3. 构建子表对象（不立即保存）
        detail_objs = []
        for detail in detail_list:
            detail_copy = detail.copy()
            detail_copy[foreign_key_field] = master_obj
            detail_objs.append(DetailModel(**detail_copy))

        # 4. 异步批量创建（Django 4.2+ 支持 abulk_create）
        try:
            created_details = await DetailModel.objects.abulk_create(detail_objs)
        except AttributeError:
            # Django < 4.2 回退到循环 acreate（性能较低）
            created_details = []
            for obj in detail_objs:
                created = await DetailModel.objects.acreate(**{f: getattr(obj, f) for f in obj._meta.fields})
                created_details.append(created)

        logger.debug(f"异步批量创建 {len(created_details)} 条 {detail_model_name} 记录")

        # 5. 【可选】金额一致性校验（使用 async 查询）
        if validate_amount_consistency and amount_field_in_master:
            if not (price_field_in_detail and quantity_field_in_detail):
                raise ValueError("启用金额校验时，必须提供 price 和 quantity 字段名")

            # 异步查询所有明细
            details = DetailModel.objects.filter(
                **{foreign_key_field: master_obj}
            ).values(price_field_in_detail, quantity_field_in_detail)

            total = Decimal('0.00')
            async for item in details:
                price = Decimal(str(item[price_field_in_detail] or 0))
                qty = Decimal(str(item[quantity_field_in_detail] or 0))
                total += price * qty

            master_amount = getattr(master_obj, amount_field_in_master, None)
            if master_amount is None:
                raise ValueError(f"主表缺少字段: {amount_field_in_master}")
            master_amount = Decimal(str(master_amount))

            if total != master_amount:
                raise ValueError(
                    f"金额不一致！主表金额: {master_amount}, 明细合计: {total}"
                )

        return master_obj


# ========================
# 异步通用事务装饰器
# ========================

def async_universal_transaction(
    *,
    model_names: List[Union[str, Tuple[str, str]]],
    timeout: float = 5.0,
    retry_times: int = 2,
    retry_delay: float = 0.5,
    allowed_exceptions: tuple = (
        # 可重试的典型数据库异常（根据实际 DB 调整）
        "deadlock",
        "Deadlock",
        "could not serialize",
        "concurrent update",
        "lock timeout",
        "Lock wait timeout",
    )
):
    """
    异步通用事务装饰器（支持超时 + 有条件重试 + 耗时统计）

    :param model_names: 模型列表，如 ["SalesOrder", ("lowcode", "SalesOrderItem")]
    :param timeout: 总超时时间（秒）
    :param retry_times: 最大重试次数
    :param retry_delay: 初始重试延迟（秒），使用指数退避
    :param allowed_exceptions: 触发重试的异常关键词（字符串片段）
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            # 标准化模型名
            resolved_models = []
            for item in model_names:
                if isinstance(item, str):
                    resolved_models.append(("lowcode", item))
                elif isinstance(item, tuple) and len(item) == 2:
                    resolved_models.append(item)
                else:
                    raise ValueError(f"无效模型标识: {item}")

            # 预验证模型是否存在
            try:
                for app_label, model_name in resolved_models:
                    apps.get_model(app_label, model_name)
            except LookupError as e:
                raise ValueError(f"事务涉及模型未注册: {e}")

            last_exception = None
            start_time = time.time()

            for attempt in range(retry_times + 1):
                try:
                    # 使用 asyncio.wait_for 实现超时控制
                    inner_start = time.time()
                    result = await asyncio.wait_for(
                        _run_atomic_async(func, *args, **kwargs),
                        timeout=timeout - (time.time() - start_time)
                    )
                    duration = time.time() - inner_start
                    logger.info(
                        f"✅ 异步事务成功 | 函数: {func.__name__} | "
                        f"耗时: {duration:.3f}s | 尝试: {attempt + 1}"
                    )
                    return result

                except asyncio.TimeoutError:
                    logger.warning("⚠️ 异步事务超时")
                    raise TimeoutError(f"异步事务执行超时（>{timeout}s）")

                except Exception as e:
                    last_exception = e
                    elapsed = time.time() - start_time
                    if elapsed >= timeout:
                        logger.warning("⚠️ 因总超时放弃重试")
                        break

                    # 判断是否可重试
                    err_msg = str(e).lower()
                    retryable = any(kw.lower() in err_msg for kw in allowed_exceptions)

                    if attempt < retry_times and retryable:
                        wait = retry_delay * (2 ** attempt)  # 指数退避
                        logger.warning(
                            f"🔄 异步事务第 {attempt + 1} 次失败（可重试）: {e}，"
                            f"{wait:.2f}s 后重试..."
                        )
                        await asyncio.sleep(wait)
                    else:
                        break

            total_duration = time.time() - start_time
            logger.error(
                f"❌ 异步事务最终失败 | 函数: {func.__name__} | "
                f"总耗时: {total_duration:.3f}s | 异常: {last_exception}"
            )
            raise last_exception

        return wrapper
    return decorator


# 辅助函数：在 transaction.atomic 中运行 async 函数
# 注意：Django 的 atomic 是同步上下文管理器，但可在 async 中使用（需 ASGI）
async def _run_atomic_async(func, *args, **kwargs):
    with transaction.atomic():
        return await func(*args, **kwargs)