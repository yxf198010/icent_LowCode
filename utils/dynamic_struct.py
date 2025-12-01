# lowcode/utils/dynamic_struct.py
"""
动态结构体工具包：
- DynamicTransaction：完全动态，无依赖，支持任意增删改查
- ValidatedDynamicTransaction：基于 Pydantic，支持类型校验（按需使用）
"""
# 通用场景（始终可用）
# from lowcode.utils.dynamic_struct import DynamicTransaction
# tx1 = DynamicTransaction(user_id=123, action="create")
# tx1.status = "done"  # ✅ 动态新增
# print(tx1.to_dict())  # {'user_id': 123, 'action': 'create', 'status': 'done'}
#
# # 校验场景（需安装 pydantic）
# try:
#     from lowcode.utils.dynamic_struct import ValidatedDynamicTransaction
#
#     class OrderTx(ValidatedDynamicTransaction):
#         user_id: int
#         amount: float
#
#     tx2 = OrderTx(user_id=456, amount=99.9, note="gift")  # ✅ extra field allowed
#     print(tx2.user_id)   # 456
#     print(tx2.to_dict()) # {'user_id': 456, 'amount': 99.9, 'note': 'gift'}
# except ImportError:
#     pass

# from lowcode.utils.dynamic_struct import ValidatedDynamicTransaction
#
# tx = ValidatedDynamicTransaction(user_id=123, name="Alice")
# print(tx.user_id)      # 123
# print(tx.to_dict())    # {'user_id': 123, 'name': 'Alice'}
import sys
from typing import Any, Dict

__all__ = ['DynamicTransaction']


# ==============================
# 1. 轻量级动态事务（推荐默认使用）
# ==============================
class DynamicTransaction:
    """
    完全动态的上下文对象，无预设字段，支持运行时任意扩展。
    不依赖任何第三方库，兼容 dataclasses.asdict()。
    """

    def __init__(self, **kwargs: Any):
        object.__setattr__(self, '__data__', kwargs)

    def __getattr__(self, name: str) -> Any:
        try:
            return self.__data__[name]
        except KeyError:
            raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")

    def __setattr__(self, name: str, value: Any) -> None:
        if name == '__data__':
            object.__setattr__(self, name, value)
        else:
            self.__data__[name] = value

    def __delattr__(self, name: str) -> None:
        try:
            del self.__data__[name]
        except KeyError:
            raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")

    def __repr__(self) -> str:
        items = ', '.join(f"{k}={v!r}" for k, v in self.__data__.items())
        return f"{self.__class__.__name__}({items})"

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.__data__)

    # 兼容 dataclasses.asdict()
    def __dataclass_fields__(self):
        return {k: None for k in self.__data__.keys()}


# ==============================
# 2. 带验证的动态事务（按需使用，需安装 pydantic >= 2.0）
# ==============================
try:
    # 尝试导入 Pydantic（即使未显式安装，也可能被其他包间接引入）
    from pydantic import BaseModel, ConfigDict

    class ValidatedDynamicTransaction(BaseModel):
        """
        支持类型校验的动态事务对象（需 Pydantic >=2.0）。
        默认允许任意额外字段，可通过继承添加字段约束。

        示例：
            class OrderTx(ValidatedDynamicTransaction):
                user_id: int
                amount: float

            tx = OrderTx(user_id=123, note="VIP")  # note 是额外字段，允许
        """
        model_config = ConfigDict(extra='allow', arbitrary_types_allowed=True)

        def to_dict(self) -> Dict[str, Any]:
            """转为标准字典（兼容其他模块）"""
            return self.model_dump()

    # 成功导入后才暴露该类
    __all__.append('ValidatedDynamicTransaction')

except ImportError:
    # Pydantic 未安装，不提供 ValidatedDynamicTransaction
    pass