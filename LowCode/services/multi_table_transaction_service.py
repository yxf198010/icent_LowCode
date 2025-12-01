# lowcode/services/multi_table_transaction_service.py
# 动态获取模型（通过 apps.get_model）
# 创建主表记录 + 多个子表明细记录
# 事务控制（@transaction.atomic）
# 支持字段映射、金额一致性校验等常见业务逻辑
# 调用示例：
# from lowcode.services.multi_table_transaction_service import MultiTableTransactionService
#
# def test_create_order():
#     master_data = {
#         "order_no": "SO20251122001",
#         "amount": 299.98,
#         "status": 1
#     }
#
#     detail_list = [
#         {"product_name": "手机", "price": 199.99, "quantity": 1},
#         {"product_name": "耳机", "price": 99.99, "quantity": 1},
#     ]
#
#     try:
#         order = MultiTableTransactionService.create_master_with_details(
#             master_model_name="SalesOrder",
#             detail_model_name="SalesOrderItem",
#             master_data=master_data,
#             detail_list=detail_list,
#             foreign_key_field="order",  # SalesOrderItem.order = SalesOrder 实例
#             amount_field_in_master="amount",
#             price_field_in_detail="price",
#             quantity_field_in_detail="quantity",
#             validate_amount_consistency=True
#         )
#         print("✅ 订单创建成功:", order.order_no)
#     except Exception as e:
#         print("❌ 事务回滚:", str(e))
# 为了满足你的需求——在现有 MultiTableTransactionService 基础上，实现支持「超时控制 + 重试机制 + 耗时统计」的通用同步多表事务装饰器，我们将：
#
# 新增一个装饰器 sync_universal_transaction
# 该装饰器支持传入模型类（或动态模型名）、超时、重试次数等参数
# 内部调用你已有的 create_master_with_details 逻辑（或其他业务函数）
# 自动处理重试、超时、日志和耗时统计
import logging
from typing import Dict, List, Optional, Any, Tuple, Union, Callable
from decimal import Decimal

from django.apps import apps
from django.db import transaction
from django.db.models import Sum, Q
from django.core.exceptions import ObjectDoesNotExist
import time
import functools
from django.core.exceptions import ValidationError


logger = logging.getLogger(__name__)


class MultiTableTransactionService:
    """
    通用多表事务服务类：
    - 主表 + 多个子表明细
    - 支持动态模型名（字符串形式）
    - 自动事务回滚
    - 可选金额一致性校验
    """

    @staticmethod
    @transaction.atomic
    def create_master_with_details(
        master_model_name: str,
        detail_model_name: str,
        master_data: Dict[str, Any],
        detail_list: List[Dict[str, Any]],
        master_app_label: str = "lowcode",
        detail_app_label: str = "lowcode",
        foreign_key_field: str = "master",  # 子表中指向主表的字段名（如 order）
        amount_field_in_master: Optional[str] = "amount",
        price_field_in_detail: Optional[str] = "price",
        quantity_field_in_detail: Optional[str] = "quantity",
        validate_amount_consistency: bool = True,
    ):
        """
        创建主表记录 + 批量子表明细（通用多表事务）

        :param master_model_name: 主表模型类名（如 'Order'）
        :param detail_model_name: 子表模型类名（如 'OrderDetail'）
        :param master_data: 主表数据字典
        :param detail_list: 子表数据列表
        :param master_app_label: 主表所在 app（默认 'lowcode'）
        :param detail_app_label: 子表所在 app
        :param foreign_key_field: 子表中关联主表的 ForeignKey 字段名（如 'order'）
        :param amount_field_in_master: 主表中的总金额字段名（用于校验）
        :param price_field_in_detail: 子表明细单价字段
        :param quantity_field_in_detail: 子表明细数量字段
        :param validate_amount_consistency: 是否校验总金额一致性
        :return: 创建成功的主表对象
        """
        # 1. 获取动态模型类
        try:
            MasterModel = apps.get_model(master_app_label, master_model_name)
            DetailModel = apps.get_model(detail_app_label, detail_model_name)
        except LookupError as e:
            raise ValueError(f"模型未注册或不存在: {e}")

        # 2. 创建主表记录
        master_obj = MasterModel.objects.create(**master_data)
        logger.debug(f"创建主表记录: {master_model_name} ID={master_obj.pk}")

        # 3. 构建子表对象列表（设置外键）
        detail_objs = []
        for detail in detail_list:
            detail_copy = detail.copy()
            # 设置外键：如 order=master_obj
            detail_copy[foreign_key_field] = master_obj
            detail_objs.append(DetailModel(**detail_copy))

        # 4. 批量创建子表记录
        created_details = DetailModel.objects.bulk_create(detail_objs)
        logger.debug(f"批量创建 {len(created_details)} 条 {detail_model_name} 记录")

        # 5. 【可选】校验金额一致性
        if validate_amount_consistency and amount_field_in_master:
            if not (price_field_in_detail and quantity_field_in_detail):
                raise ValueError("启用金额校验时，必须提供 price 和 quantity 字段名")

            # 使用聚合计算明细总金额：SUM(price * quantity)
            # 注意：Django 不直接支持 SUM(price * quantity)，需用 extra 或 annotate
            details = DetailModel.objects.filter(
                **{foreign_key_field: master_obj}
            ).values(price_field_in_detail, quantity_field_in_detail)

            total = Decimal('0.00')
            for item in details:
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



# # 示例：创建订单业务函数（使用装饰器）
#
# @sync_universal_transaction(
#     model_names=[
#         ("lowcode", "SalesOrder"),
#         ("lowcode", "SalesOrderItem")
#     ],
#     timeout=5.0,
#     retry_times=3,
#     retry_delay=0.5
# )
# def create_sales_order_business(master_data: dict, detail_list: list):
#     return MultiTableTransactionService.create_master_with_details(
#         master_model_name="SalesOrder",
#         detail_model_name="SalesOrderItem",
#         master_data=master_data,
#         detail_list=detail_list,
#         foreign_key_field="order",
#         amount_field_in_master="amount",
#         price_field_in_detail="price",
#         quantity_field_in_detail="quantity",
#         validate_amount_consistency=True
#     )
#
#
# # 调用方式（与之前一致）
# def test_create_order():
#     master_data = {
#         "order_no": "SO20251122001",
#         "amount": 299.98,
#         "status": 1
#     }
#     detail_list = [
#         {"product_name": "手机", "price": 199.99, "quantity": 1},
#         {"product_name": "耳机", "price": 99.99, "quantity": 1},
#     ]
#
#     try:
#         order = create_sales_order_business(master_data, detail_list)
#         print("✅ 订单创建成功:", order.order_no)
#     except Exception as e:
#         print("❌ 事务最终失败:", str(e))

def sync_universal_transaction(
        *,
        model_names: List[Union[str, Tuple[str, str]]],
        timeout: float = 5.0,
        retry_times: int = 2,
        retry_delay: float = 0.5,
        isolation_level: str = None  # 如需扩展可加，此处暂不实现 DB 级隔离设置
):
    """
    同步通用事务装饰器（支持超时 + 重试 + 耗时统计）

    :param model_names: 模型标识列表，支持：
        - 字符串: "SalesOrder" → 默认 app='lowcode'
        - 元组: ("myapp", "Order")
    :param timeout: 事务最大执行时间（秒），超时则中断并回滚
    :param retry_times: 失败后重试次数（仅对数据库冲突类异常重试）
    :param retry_delay: 重试间隔（秒）
    :param isolation_level: （预留）事务隔离级别，如 'READ COMMITTED'
    """

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        def wrapper(*args, **kwargs) -> Any:
            # 标准化 model_names 为 (app_label, model_name) 列表
            resolved_models = []
            for item in model_names:
                if isinstance(item, str):
                    resolved_models.append(("lowcode", item))
                elif isinstance(item, tuple) and len(item) == 2:
                    resolved_models.append(item)
                else:
                    raise ValueError(f"无效的模型标识: {item}")

            # 预加载模型（提前验证是否存在）
            try:
                for app_label, model_name in resolved_models:
                    apps.get_model(app_label, model_name)
            except LookupError as e:
                raise ValueError(f"事务涉及的模型未注册: {e}")

            last_exception = None
            start_time = time.time()

            for attempt in range(retry_times + 1):
                try:
                    # 使用独立事务块（避免外层干扰）
                    with transaction.atomic():
                        # 记录子事务开始时间
                        inner_start = time.time()
                        result = func(*args, **kwargs)
                        duration = time.time() - inner_start

                        # 超时检查（虽然在 atomic 内，但 Python 层可检测）
                        total_elapsed = time.time() - start_time
                        if total_elapsed > timeout:
                            raise TimeoutError(f"事务执行超时（>{timeout}s）")

                        logger.info(
                            f"✅ 事务成功 | 函数: {func.__name__} | "
                            f"耗时: {duration:.3f}s | 尝试次数: {attempt + 1}"
                        )
                        return result

                except (TimeoutError, KeyboardInterrupt):
                    # 不可重试的致命错误
                    logger.error("❌ 事务被强制中断（超时或用户取消）")
                    raise

                except Exception as e:
                    last_exception = e
                    total_elapsed = time.time() - start_time
                    if total_elapsed > timeout:
                        logger.warning("⚠️ 事务因超时放弃重试")
                        break

                    # 可重试的数据库异常（根据实际 DB 调整）
                    retryable = any(
                        msg in str(e)
                        for msg in [
                            "deadlock", "Deadlock", "could not serialize",
                            "concurrent update", "lock", "timeout"
                        ]
                    )

                    if attempt < retry_times and retryable:
                        wait = retry_delay * (2 ** attempt)  # 指数退避（可选）
                        logger.warning(
                            f"🔄 事务第 {attempt + 1} 次失败（可重试）: {e}，"
                            f"{wait:.2f}s 后重试..."
                        )
                        time.sleep(wait)
                    else:
                        # 不可重试 or 已达最大重试次数
                        break

            # 所有重试失败 or 超时
            total_duration = time.time() - start_time
            logger.error(
                f"❌ 事务最终失败 | 函数: {func.__name__} | "
                f"总耗时: {total_duration:.3f}s | 最后异常: {last_exception}"
            )
            raise last_exception

        return wrapper

    return decorator