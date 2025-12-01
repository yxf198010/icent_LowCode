# lowcode/services/orderservice_dome.py
# 使用 @transaction.atomic 装饰器包裹多表操作（Order + OrderDetail）；
# 先创建主表 Order，再用外键关联批量创建从表 OrderDetail；
# 调用模型自定义方法（如 order.calculate_total()）进行业务校验；
# 强调“Django 模型方式无需手动管理事务、外键关联、SQL拼接”；
# 将此类逻辑封装在 services.py 中，作为业务层服务类（如 OrderService）
from ..utils.universal_transaction import universal_transaction
# from ..models.static_models import Order, OrderDetail
#
# class OrderService:
#     @staticmethod
#     @universal_transaction(models=[Order, OrderDetail], retry_times=2)
#     def create_order(order_data, detail_list):
#         order = Order.objects.create(**order_data)
#         details = [OrderDetail(order=order, **item) for item in detail_list]
#         OrderDetail.objects.bulk_create(details)
#
#         total = order.calculate_total()
#         if total != order.amount:
#             raise ValueError("金额不一致")
#
#         return order