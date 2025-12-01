# lowcode/models/dynamic_model_specs.py
"""
动态模型方法模板系统：提供可复用的逻辑模板（聚合、字段更新、自定义函数），
供动态方法绑定器调用。模板本身无权限/日志，由外层封装注入。
"""
# 存储所有动态模型的 元数据定义（字段、索引、方法模板、权限等）
import logging
from typing import Any
from django.db.models import Sum, Avg, Count, Max, Min, F
from importlib import import_module
import os

logger = logging.getLogger(__name__)


# ==================== 聚合操作模板 ====================

def _aggregate_template(self, params: dict) -> float:
    """
    聚合计算模板：支持 sum/avg/count/max/min，可选乘法字段（如 price * quantity）

    配置示例：
    {
        "related_name": "order_lines",
        "agg_field": "price",
        "multiply_field": "quantity",  # 可选
        "operation": "sum"
    }
    """
    related_name = params.get("related_name")
    agg_field = params.get("agg_field") or params.get("aggregate_field")  # 兼容旧字段
    multiply_field = params.get("multiply_field")
    operation = (params.get("operation") or "sum").lower()

    if not related_name or not agg_field:
        raise ValueError("缺少必要参数: 'related_name' 和 'agg_field'")

    op_map = {"sum": Sum, "avg": Avg, "count": Count, "max": Max, "min": Min}
    agg_func = op_map.get(operation)
    if not agg_func:
        raise ValueError(f"不支持的聚合操作 '{operation}'，支持: {list(op_map.keys())}")

    try:
        related_manager = getattr(self, related_name)
        if multiply_field:
            expr = F(agg_field) * F(multiply_field)
            result = related_manager.aggregate(total=agg_func(expr))['total']
        else:
            result = related_manager.aggregate(total=agg_func(agg_field))['total']
        return result if result is not None else 0
    except Exception as e:
        model_name = self.__class__.__name__
        logger.error(
            f"[聚合模板] 执行失败 | 模型={model_name} | 方法=aggregate | "
            f"related={related_name} | field={agg_field} | op={operation} | 错误={e}",
            exc_info=True
        )
        raise


# ==================== 字段更新模板 ====================

def _field_update_template(self, params: dict, new_value: Any) -> Any:
    """
    字段更新模板：设置指定字段并保存

    配置示例：
    {
        "target_field": "status"
    }
    """
    target_field = params.get("target_field") or params.get("field_name")
    if not target_field:
        raise ValueError("缺少必要参数: 'target_field'")
    if not hasattr(self, target_field):
        model_name = self.__class__.__name__
        raise AttributeError(f"模型 '{model_name}' 无字段 '{target_field}'")

    setattr(self, target_field, new_value)
    self.save(update_fields=[target_field])
    return getattr(self, target_field)


# ==================== 自定义函数模板 ====================

def _custom_func_template(self, params: dict, *args, **kwargs) -> Any:
    """
    自定义函数模板：动态导入并执行外部函数

    配置示例：
    {
        "func_path": "myapp.utils.calculate_discount"
    }
    函数签名应为: func(instance, *args, **kwargs)
    """
    ALLOWED_MODULES = {"myapp.utils", "common.helpers"}

    func_path = params.get("func_path")
    if not func_path:
        raise ValueError("缺少必要参数: 'func_path'（格式如 'myapp.utils.my_func'）")

    #白名单校验：暂不校验
    # ALLOWED_MODULES = {"myapp.utils", "common.helpers"}
    # module_path, _ = func_path.rsplit(".", 1)
    # if not any(module_path.startswith(prefix) for prefix in ALLOWED_MODULES):
    #     raise ValueError("不允许调用该模块的函数")

    try:
        module_path, func_name = func_path.rsplit(".", 1)
        module = import_module(module_path)
        custom_func = getattr(module, func_name)
        if not callable(custom_func):
            raise TypeError(f"'{func_path}' 不是可调用对象")
        return custom_func(self, *args, **kwargs)
    except Exception as e:
        model_name = self.__class__.__name__
        logger.error(
            f"[自定义函数模板] 调用失败 | 模型={model_name} | func_path={func_path} | 错误={e}",
            exc_info=True
        )
        raise


# ==================== 模板映射（供绑定器使用） ====================
METHOD_TEMPLATES = {
    "aggregate": _aggregate_template,
    "field_update": _field_update_template,
    "custom_func": _custom_func_template,
}


# ==================== （可选）从文件加载配置 ====================
def load_method_configs_from_file(file_path: str) -> list:
    """
    从 YAML/JSON 文件加载方法配置（用于混合配置场景）
    返回格式: [{"method_name": "...", "method_type": "...", "config": {...}}, ...]
    """
    if not os.path.exists(file_path):
        logger.warning(f"配置文件不存在: {file_path}")
        return []

    try:
        if file_path.endswith(('.yaml', '.yml')):
            import yaml
            with open(file_path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f) or []
        elif file_path.endswith('.json'):
            import json
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
        else:
            raise ValueError("仅支持 .yaml/.yml 或 .json 格式")
        return data
    except Exception as e:
        logger.error(f"加载配置文件失败: {file_path} | 错误: {e}", exc_info=True)
        return []




#   动态数据容器类，用于在运行时接收任意关键字参数并支持属性访问和字典转换
# def _custom_func_template(instance, params: dict, *args, **kwargs):
#     # 构建动态上下文
#     context = DynamicTransaction(
#         model_instance=instance,
#         method_params=params,
#         user_input_args=args,
#         user_input_kwargs=kwargs,
#         timestamp=datetime.now(),
#     )
#
#     # 执行自定义逻辑（伪代码）
#     result = your_custom_logic(context)
#     return result