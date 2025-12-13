"""
动态模型方法模板系统：提供可复用的逻辑模板（聚合、字段更新、自定义函数），
供动态方法绑定器调用。模板本身无权限/日志，由外层封装注入。
"""
import logging
import os
import json
from typing import Any, Dict, Callable, Optional, Union, List
from datetime import datetime
from dataclasses import dataclass, asdict
from importlib import import_module
from django.conf import settings
from django.db.models import Sum, Avg, Count, Max, Min, F, QuerySet
from django.db import transaction
from django.core.exceptions import FieldDoesNotExist

logger = logging.getLogger(__name__)

# ==================== 核心配置（可通过Django Settings覆盖） ====================
# 自定义函数白名单前缀（生产环境必须配置）
ALLOWED_FUNC_MODULE_PREFIXES = getattr(
    settings,
    "LOWCODE_ALLOWED_FUNC_MODULES",
    ["myapp.utils.", "common.helpers.", "lowcode.methods."]
)

# 聚合操作重试次数（针对并发场景）
AGGREGATE_RETRY_TIMES = getattr(settings, "LOWCODE_AGGREGATE_RETRY", 1)

# 配置文件支持的格式
SUPPORTED_CONFIG_FORMATS = (".json", ".yaml", ".yml")

# ==================== 类型定义 ====================
AggregateParams = Dict[str, Union[str, int, float]]
FieldUpdateParams = Dict[str, str]
CustomFuncParams = Dict[str, str]
MethodParams = Union[AggregateParams, FieldUpdateParams, CustomFuncParams]


# ==================== 动态上下文容器（统一参数传递） ====================
@dataclass
class DynamicMethodContext:
    """
    动态方法执行上下文：封装所有运行时参数，支持属性访问和字典转换
    """
    model_instance: Any  # 动态模型实例
    method_params: MethodParams  # 方法配置参数
    args: tuple  # 调用时传入的位置参数
    kwargs: dict  # 调用时传入的关键字参数
    timestamp: datetime = datetime.now()
    task_id: Optional[str] = None  # 可选任务ID（异步场景）
    user_id: Optional[int] = None  # 可选用户ID（权限审计）

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典（便于日志/序列化）"""
        return asdict(self)

    def get_model_name(self) -> str:
        """获取模型名称"""
        return self.model_instance.__class__.__name__

    def get_table_name(self) -> str:
        """获取数据表名"""
        return self.model_instance._meta.db_table


# ==================== 工具函数 ====================
def validate_func_path(func_path: str) -> bool:
    """
    校验自定义函数路径是否在白名单内
    :param func_path: 如 "myapp.utils.calculate_discount"
    :return: 是否合法
    """
    if not func_path or "." not in func_path:
        return False
    return any(func_path.startswith(prefix) for prefix in ALLOWED_FUNC_MODULE_PREFIXES)


def load_module_func(func_path: str) -> Callable:
    """
    安全加载模块中的函数
    :param func_path: 函数路径
    :return: 可调用函数
    :raises: ImportError / AttributeError / TypeError
    """
    if not validate_func_path(func_path):
        raise ValueError(
            f"函数路径 '{func_path}' 不在白名单内，允许前缀：{ALLOWED_FUNC_MODULE_PREFIXES}"
        )

    try:
        module_path, func_name = func_path.rsplit(".", 1)
        module = import_module(module_path)
        func = getattr(module, func_name)
        if not callable(func):
            raise TypeError(f"'{func_path}' 不是可调用对象")
        return func
    except (ValueError, ImportError, AttributeError) as e:
        raise ImportError(f"加载函数失败 '{func_path}': {str(e)}") from e


# ==================== 聚合操作模板（强化版） ====================
def _aggregate_template(context: DynamicMethodContext) -> float:
    """
    聚合计算模板：支持 sum/avg/count/max/min，可选乘法字段（如 price * quantity）
    增加重试机制、事务支持、完善的错误处理

    配置示例：
    {
        "related_name": "order_lines",
        "agg_field": "price",
        "multiply_field": "quantity",  # 可选
        "operation": "sum"
    }
    """
    params = context.method_params
    related_name = params.get("related_name")
    agg_field = params.get("agg_field") or params.get("aggregate_field")  # 兼容旧字段
    multiply_field = params.get("multiply_field")
    operation = (params.get("operation") or "sum").lower()

    # 基础参数校验
    if not related_name or not agg_field:
        raise ValueError("聚合模板缺少必要参数: 'related_name' 和 'agg_field'")

    # 聚合函数映射
    op_map = {
        "sum": Sum,
        "avg": Avg,
        "count": Count,
        "max": Max,
        "min": Min
    }
    agg_func = op_map.get(operation)
    if not agg_func:
        raise ValueError(f"不支持的聚合操作 '{operation}'，支持: {list(op_map.keys())}")

    # 重试机制执行聚合
    model_instance = context.model_instance
    model_name = context.get_model_name()
    last_error = None

    for retry in range(AGGREGATE_RETRY_TIMES + 1):
        try:
            with transaction.atomic():
                # 获取关联管理器
                if not hasattr(model_instance, related_name):
                    raise AttributeError(
                        f"模型 '{model_name}' 无关联属性 '{related_name}'"
                    )
                related_manager: QuerySet = getattr(model_instance, related_name)

                # 构建聚合表达式
                if multiply_field:
                    # 校验字段是否存在
                    try:
                        related_manager.model._meta.get_field(agg_field)
                        related_manager.model._meta.get_field(multiply_field)
                    except FieldDoesNotExist as e:
                        raise ValueError(f"聚合字段不存在: {str(e)}") from e
                    expr = F(agg_field) * F(multiply_field)
                    result = related_manager.aggregate(total=agg_func(expr))['total']
                else:
                    # 校验聚合字段
                    try:
                        related_manager.model._meta.get_field(agg_field)
                    except FieldDoesNotExist as e:
                        raise ValueError(f"聚合字段不存在: {str(e)}") from e
                    result = related_manager.aggregate(total=agg_func(agg_field))['total']

                # 日志记录成功
                logger.info(
                    f"[聚合模板] 执行成功 | 模型={model_name} | 关联={related_name} | "
                    f"字段={agg_field} | 操作={operation} | 结果={result} | "
                    f"重试次数={retry} | 任务ID={context.task_id}"
                )
                return result if result is not None else 0

        except Exception as e:
            last_error = e
            logger.warning(
                f"[聚合模板] 执行失败（重试{retry}/{AGGREGATE_RETRY_TIMES}）| "
                f"模型={model_name} | 关联={related_name} | 字段={agg_field} | "
                f"错误={str(e)} | 任务ID={context.task_id}"
            )
            if retry >= AGGREGATE_RETRY_TIMES:
                break

    # 所有重试失败，抛出最终错误
    logger.error(
        f"[聚合模板] 所有重试失败 | 模型={model_name} | 关联={related_name} | "
        f"字段={agg_field} | 操作={operation} | 最终错误={str(last_error)} | "
        f"任务ID={context.task_id}",
        exc_info=True
    )
    raise last_error


# ==================== 字段更新模板（强化版） ====================
def _field_update_template(context: DynamicMethodContext) -> Any:
    """
    字段更新模板：设置指定字段并保存，支持事务、字段校验、部分更新

    配置示例：
    {
        "target_field": "status",
        "allow_null": False  # 可选，是否允许设置为None
    }
    """
    params = context.method_params
    target_field = params.get("target_field") or params.get("field_name")
    allow_null = params.get("allow_null", False)
    model_instance = context.model_instance
    model_name = context.get_model_name()

    # 基础参数校验
    if not target_field:
        raise ValueError("字段更新模板缺少必要参数: 'target_field'")

    # 校验字段是否存在
    try:
        model_instance._meta.get_field(target_field)
    except FieldDoesNotExist as e:
        raise AttributeError(f"模型 '{model_name}' 无字段 '{target_field}'") from e

    # 获取新值（从kwargs中取，key为target_field）
    new_value = context.kwargs.get(target_field)
    if new_value is None and not allow_null:
        raise ValueError(f"字段 '{target_field}' 不允许设置为None（allow_null=False）")

    # 事务中更新字段
    try:
        with transaction.atomic():
            setattr(model_instance, target_field, new_value)
            # 仅更新指定字段，提升性能
            model_instance.save(update_fields=[target_field])
            updated_value = getattr(model_instance, target_field)

            logger.info(
                f"[字段更新模板] 执行成功 | 模型={model_name} | 字段={target_field} | "
                f"旧值={getattr(model_instance, target_field, None)} | 新值={updated_value} | "
                f"任务ID={context.task_id} | 用户ID={context.user_id}"
            )
            return updated_value

    except Exception as e:
        logger.error(
            f"[字段更新模板] 执行失败 | 模型={model_name} | 字段={target_field} | "
            f"新值={new_value} | 错误={str(e)} | 任务ID={context.task_id}",
            exc_info=True
        )
        raise


# ==================== 自定义函数模板（安全强化版） ====================
def _custom_func_template(context: DynamicMethodContext) -> Any:
    """
    自定义函数模板：动态导入并执行外部函数，强化安全校验和上下文传递

    配置示例：
    {
        "func_path": "myapp.utils.calculate_discount",
        "timeout": 30  # 可选，函数执行超时时间（秒）
    }
    函数签名应为: func(context: DynamicMethodContext, *args, **kwargs)
    """
    params = context.method_params
    func_path = params.get("func_path")
    model_name = context.get_model_name()

    # 基础参数校验
    if not func_path:
        raise ValueError("自定义函数模板缺少必要参数: 'func_path'（格式如 'myapp.utils.my_func'）")

    try:
        # 安全加载函数
        custom_func = load_module_func(func_path)

        # 执行自定义函数（传递完整上下文）
        logger.info(
            f"[自定义函数模板] 开始执行 | 模型={model_name} | 函数路径={func_path} | "
            f"任务ID={context.task_id} | 用户ID={context.user_id}"
        )

        result = custom_func(context, *context.args, **context.kwargs)

        logger.info(
            f"[自定义函数模板] 执行成功 | 模型={model_name} | 函数路径={func_path} | "
            f"结果类型={type(result).__name__} | 任务ID={context.task_id}"
        )
        return result

    except Exception as e:
        logger.error(
            f"[自定义函数模板] 执行失败 | 模型={model_name} | 函数路径={func_path} | "
            f"错误={str(e)} | 任务ID={context.task_id}",
            exc_info=True
        )
        raise


# ==================== 模板映射（供绑定器使用） ====================
METHOD_TEMPLATES: Dict[str, Callable[[DynamicMethodContext], Any]] = {
    "aggregate": _aggregate_template,
    "field_update": _field_update_template,
    "custom_func": _custom_func_template,
}


# ==================== 配置加载（强化版） ====================
def load_method_configs_from_file(file_path: str) -> List[Dict[str, Any]]:
    """
    从 YAML/JSON 文件加载方法配置（支持编码检测、格式校验、错误处理）
    返回格式: [{"method_name": "...", "method_type": "...", "config": {...}}, ...]
    """
    # 基础校验
    if not os.path.exists(file_path):
        logger.warning(f"[配置加载] 文件不存在: {file_path}")
        return []

    file_ext = os.path.splitext(file_path)[1].lower()
    if file_ext not in SUPPORTED_CONFIG_FORMATS:
        logger.error(
            f"[配置加载] 不支持的文件格式: {file_ext} | 文件={file_path} | "
            f"支持格式={SUPPORTED_CONFIG_FORMATS}"
        )
        return []

    # 加载配置文件
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            if file_ext in (".yaml", ".yml"):
                import yaml
                # 安全加载YAML，禁止执行任意代码
                data = yaml.safe_load(f) or []
            elif file_ext == ".json":
                data = json.load(f)
            else:
                data = []

        # 校验配置格式
        if not isinstance(data, list):
            logger.error(f"[配置加载] 配置文件根节点必须是列表 | 文件={file_path}")
            return []

        # 校验每个配置项的必填字段
        valid_configs = []
        for idx, config in enumerate(data):
            if not isinstance(config, dict):
                logger.warning(f"[配置加载] 第{idx}项配置不是字典，跳过 | 文件={file_path}")
                continue

            # 校验必填字段
            required_fields = ["method_name", "method_type", "config"]
            missing = [f for f in required_fields if f not in config]
            if missing:
                logger.warning(
                    f"[配置加载] 第{idx}项配置缺少字段 {missing}，跳过 | 文件={file_path}"
                )
                continue

            # 校验方法类型是否支持
            method_type = config.get("method_type")
            if method_type not in METHOD_TEMPLATES:
                logger.warning(
                    f"[配置加载] 第{idx}项配置不支持的方法类型 '{method_type}'，跳过 | "
                    f"文件={file_path} | 支持类型={list(METHOD_TEMPLATES.keys())}"
                )
                continue

            valid_configs.append(config)

        logger.info(
            f"[配置加载] 成功加载 {len(valid_configs)}/{len(data)} 个有效配置 | "
            f"文件={file_path}"
        )
        return valid_configs

    except UnicodeDecodeError as e:
        logger.error(f"[配置加载] 文件编码错误 | 文件={file_path} | 错误={str(e)}")
        return []
    except Exception as e:
        logger.error(
            f"[配置加载] 加载文件失败 | 文件={file_path} | 错误={str(e)}",
            exc_info=True
        )
        return []


# ==================== 便捷调用封装（可选） ====================
def execute_dynamic_method(
    model_instance: Any,
    method_type: str,
    method_params: MethodParams,
    *args,
    **kwargs
) -> Any:
    """
    便捷封装：创建上下文并执行指定类型的动态方法

    :param model_instance: 动态模型实例
    :param method_type: 方法类型（aggregate/field_update/custom_func）
    :param method_params: 方法配置参数
    :param args: 位置参数
    :param kwargs: 关键字参数
    :return: 方法执行结果
    """
    # 校验方法类型
    if method_type not in METHOD_TEMPLATES:
        raise ValueError(f"不支持的方法类型 '{method_type}'，支持: {list(METHOD_TEMPLATES.keys())}")

    # 创建执行上下文
    context = DynamicMethodContext(
        model_instance=model_instance,
        method_params=method_params,
        args=args,
        kwargs=kwargs,
        task_id=kwargs.pop("task_id", None),
        user_id=kwargs.pop("user_id", None)
    )

    # 执行方法模板
    template_func = METHOD_TEMPLATES[method_type]
    return template_func(context)