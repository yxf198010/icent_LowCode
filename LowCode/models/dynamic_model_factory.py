# lowcode/models/dynamic_model_factory.py
"""
动态模型工厂：运行时创建 Django 模型类，并绑定带权限/日志的自定义方法。
整合了字段构建、系统模型引用、命名校验、默认字段注入、方法绑定、热更新等能力。
支持三种标准入口：
- get_dynamic_model_by_id(model_id: int)
- get_dynamic_model_by_name(model_name: str)   ← 推荐
- get_dynamic_model_by_config(model_name, fields, table_name=None)

新增安全注册表接口（推荐使用）：
- get_dynamic_model(model_name: str)
- list_dynamic_model_names()
- get_all_dynamic_models()
"""
# 底层核心：模型构建、字段校验、方法绑定、缓存 / 锁管理、表验证
# # 获取动态模型
# user_model = get_dynamic_model("User")
#
# # 导出数据（需传入用户对象用于权限校验）
# from django.contrib.auth import get_user_model
# admin_user = get_user_model().objects.get(username='admin')
#
# # 导出所有字段
# user_model.export_to_csv(
#     admin_user,
#     file_path="/tmp/user_all.csv",
#     encoding="gbk"  # 适配Excel打开乱码
# )
#
# # 导出指定字段
# user_model.export_to_csv(
#     admin_user,
#     file_path="/tmp/user_simple.csv",
#     fields=["name", "phone", "create_time"]
# )

import re
import logging
import hashlib
import keyword
import json
import csv
from datetime import datetime, date
from typing import Any, Dict, List, Optional, Type, Union, Callable, Set, Tuple
from django.db import models, connection
from django.apps import apps
from django.core.exceptions import (
    AppRegistryNotReady,
    FieldError,
    ObjectDoesNotExist,
    PermissionDenied  # 新增：统一使用Django标准权限异常
)
from django.db.models import F, Sum, Avg, Count, Max, Min
from django.utils import timezone
from django.core.management.color import no_style
from django.db.backends.utils import truncate_name
import time
import importlib
from threading import Lock, RLock

# 修正导入路径 + 补充缺失的导入
from .. import dynamic_model_registry
from ..utils.permission import check_method_permission, check_data_permission
from ..utils.log import record_method_call_log
from lowcode.models.models import LowCodeModelConfig, MethodLowCode, LowCodeMethodCallLog

logger = logging.getLogger(__name__)

# ==================== 常量定义 ====================

RESERVED_FIELD_NAMES = {
    'id', 'pk', 'objects', 'save', 'delete', 'clean', 'full_clean',
    'serializable_value', '_state', '_meta',
    'create_time', 'update_time',
    "DoesNotExist", "MultipleObjectsReturned",
}

FIELD_NAME_PATTERN = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')

SUPPORTED_FIELD_TYPES = {
    # 标准 Django 类型
    "CharField": models.CharField,
    "TextField": models.TextField,
    "IntegerField": models.IntegerField,
    "BigIntegerField": models.BigIntegerField,
    "SmallIntegerField": models.SmallIntegerField,
    "FloatField": models.FloatField,
    "DecimalField": models.DecimalField,
    "BooleanField": models.BooleanField,
    "DateTimeField": models.DateTimeField,
    "DateField": models.DateField,
    "EmailField": models.EmailField,
    "URLField": models.URLField,
    "JSONField": models.JSONField,
    "UUIDField": models.UUIDField,
    # 别名映射
    "char": ("CharField", {"max_length": 200}),
    "text": ("TextField", {}),
    "integer": ("IntegerField", {}),
    "float": ("FloatField", {}),
    "boolean": ("BooleanField", {}),
    "date": ("DateField", {}),
    "datetime": ("DateTimeField", {}),
    "email": ("EmailField", {"max_length": 254}),
    "url": ("URLField", {"max_length": 200}),
    "choice": ("CharField", {"max_length": 200}),
}

SUPPORTED_FOREIGN_KEY_MODELS = {
    'User': ('auth', 'user'),
    'Role': ('lowcode', 'role'),
    'LowCodeUser': ('lowcode', 'lowcodeuser'),
}

COMMON_FIELD_KWARGS = {'verbose_name', 'help_text', 'null', 'blank', 'default', 'unique'}

FIELD_TYPE_KWARGS = {
    "CharField": {"max_length"},
    "TextField": set(),
    "IntegerField": set(),
    "BigIntegerField": set(),
    "SmallIntegerField": set(),
    "FloatField": set(),
    "DecimalField": {"max_digits", "decimal_places"},
    "BooleanField": set(),
    "DateTimeField": {"auto_now", "auto_now_add"},
    "DateField": {"auto_now", "auto_now_add"},
    "EmailField": {"max_length"},
    "URLField": {"max_length"},
    "JSONField": set(),
    "UUIDField": set(),
}

# ==================== 内部状态管理（安全封装） ====================

# 构建缓存：基于字段配置生成的模型（用于去重）
_DYNAMIC_MODEL_CACHE: Dict[str, Type[models.Model]] = {}

# 注册表：按模型名称注册的模型（用于快速查找）
_DYNAMIC_MODEL_REGISTRY: Dict[str, Type[models.Model]] = {}

# 加载状态控制
_DYNAMIC_MODELS_LOADED = False
_LOAD_LOCK = RLock()

# 方法绑定状态
DYNAMIC_METHOD_PREFIX = "_dyn_method_"
_BOUND_DYNAMIC_METHODS: Set[Tuple[str, str]] = set()
_BIND_LOCK = RLock()


def _is_registry_loaded() -> bool:
    return _DYNAMIC_MODELS_LOADED


def ensure_dynamic_models_loaded():
    """确保所有动态模型已加载到注册表（幂等、线程安全）"""
    global _DYNAMIC_MODELS_LOADED
    if _DYNAMIC_MODELS_LOADED:
        return

    with _LOAD_LOCK:
        if _DYNAMIC_MODELS_LOADED:
            return
        try:
            _load_all_dynamic_models_into_registry()
            _DYNAMIC_MODELS_LOADED = True
            logger.info("✅ 动态模型注册表加载完成")
        except Exception as e:
            logger.error(f"❌ 动态模型注册表加载失败: {e}", exc_info=True)
            raise


def _load_all_dynamic_models_into_registry():
    """
    从数据库加载所有动态模型配置并注册到注册表
    """
    model_configs = (
        LowCodeModelConfig.objects
        .annotate(field_count=Count('fields'))
        .exclude(field_count=0)
        .prefetch_related('fields')  # 优化查询
        .order_by('name')
    )

    for config in model_configs:
        try:
            # 获取字段配置：适配 ForeignKey 到 FieldLowCode 模型的场景
            field_configs = [
                {
                    'name': f.name,
                    'type': f.field_type,
                    'label': f.label,
                    'required': f.required,
                    'null': f.null,
                    'blank': f.blank,
                    'default': f.default,
                    'max_length': f.max_length,
                    'options': f.options,
                    'help_text': f.help_text,
                    'to': f.to_model,  # 适配 ForeignKey 关联模型
                    'on_delete': f.on_delete,
                }
                for f in config.fields.all()
            ]

            table_name = config.table_name or f"lowcode_{config.name.lower()}"
            dynamic_model = create_dynamic_model(
                model_name=config.name,
                fields=field_configs,
                table_name=table_name
            )

            # 注册到内部注册表
            _DYNAMIC_MODEL_REGISTRY[config.name] = dynamic_model

            # 绑定方法（包含导出方法）
            bind_methods_to_model(dynamic_model, force_rebind=False)

        except Exception as e:
            logger.error(f"注册动态模型 {config.name} 失败: {str(e)}", exc_info=True)


# ==================== 安全公共接口（推荐使用） ====================

def get_dynamic_model(model_name: str) -> Optional[Type[models.Model]]:
    """安全获取单个动态模型（自动触发懒加载）"""
    ensure_dynamic_models_loaded()
    return _DYNAMIC_MODEL_REGISTRY.get(model_name)


def list_dynamic_model_names() -> List[str]:
    """安全获取所有动态模型名称（只读）"""
    ensure_dynamic_models_loaded()
    return list(_DYNAMIC_MODEL_REGISTRY.keys())


def get_all_dynamic_models() -> Dict[str, Type[models.Model]]:
    """返回动态模型注册表的只读副本（防止外部修改）"""
    ensure_dynamic_models_loaded()
    return _DYNAMIC_MODEL_REGISTRY.copy()


# ==================== 字段构建 ====================

def _is_valid_field_name(name: str) -> bool:
    if not name or not isinstance(name, str):
        return False
    if len(name) > 63:
        logger.warning(f"字段名 '{name}' 超过 63 字符")
        return False
    if not FIELD_NAME_PATTERN.match(name):
        return False
    if keyword.iskeyword(name):
        return False
    return True


def _resolve_foreign_key_target(to_model_name: str) -> models.Model:
    if to_model_name not in SUPPORTED_FOREIGN_KEY_MODELS:
        raise ValueError(
            f"不支持的关联模型 '{to_model_name}'。"
            f"支持的模型: {list(SUPPORTED_FOREIGN_KEY_MODELS.keys())}"
        )
    app_label, model_name = SUPPORTED_FOREIGN_KEY_MODELS[to_model_name]
    try:
        return apps.get_model(app_label, model_name)
    except LookupError as e:
        raise ValueError(f"无法加载模型 '{to_model_name}': {e}")


def _build_field(field_config: Dict[str, Any]) -> models.Field:
    name = field_config.get("name")
    field_type = field_config.get("type")

    if not name or not field_type:
        raise ValueError("字段必须包含 'name' 和 'type'")

    if not _is_valid_field_name(name):
        raise ValueError(f"字段名 '{name}' 不是合法的 Python 标识符")

    if name in RESERVED_FIELD_NAMES:
        raise ValueError(f"字段名 '{name}' 是系统保留字段，禁止使用")

    if field_type == "ForeignKey" or field_type == "foreignkey":
        to_model_name = field_config.get("to")
        if not to_model_name:
            raise ValueError("ForeignKey 必须指定 'to' 参数")
        target_model = _resolve_foreign_key_target(to_model_name)
        kwargs = {
            "to": target_model,
            "on_delete": models.CASCADE,
            "verbose_name": name.replace('_', ' ').title()
        }

        if field_config.get("null") or field_config.get("required") is False:
            kwargs["null"] = True
            kwargs["blank"] = True
            kwargs["on_delete"] = models.SET_NULL

        on_delete_str = field_config.get("on_delete")
        if isinstance(on_delete_str, str):
            on_delete_map = {
                'CASCADE': models.CASCADE,
                'SET_NULL': models.SET_NULL,
                'PROTECT': models.PROTECT,
                'DO_NOTHING': models.DO_NOTHING,
            }
            kwargs["on_delete"] = on_delete_map.get(on_delete_str, models.CASCADE)

        return models.ForeignKey(**kwargs)

    actual_type = field_type
    extra_defaults = {}
    if field_type in SUPPORTED_FIELD_TYPES and isinstance(SUPPORTED_FIELD_TYPES[field_type], tuple):
        actual_type, extra_defaults = SUPPORTED_FIELD_TYPES[field_type]

    if actual_type not in SUPPORTED_FIELD_TYPES or isinstance(SUPPORTED_FIELD_TYPES[actual_type], tuple):
        raise ValueError(
            f"不支持的字段类型 '{field_type}'。支持类型: {list(k for k in SUPPORTED_FIELD_TYPES if not isinstance(SUPPORTED_FIELD_TYPES[k], tuple))}"
        )

    field_class = SUPPORTED_FIELD_TYPES[actual_type]
    kwargs = extra_defaults.copy()

    for key in COMMON_FIELD_KWARGS:
        if key in field_config:
            kwargs[key] = field_config[key]

    if "label" in field_config and "verbose_name" not in kwargs:
        kwargs["verbose_name"] = field_config["label"]

    if field_config.get("required") is False:
        kwargs.setdefault("blank", True)
        if actual_type != "BooleanField":
            kwargs.setdefault("null", True)

    if actual_type == "CharField" and field_config.get("type") == "choice" and "options" in field_config:
        kwargs["choices"] = [(opt, opt) for opt in field_config["options"]]

    allowed_extra = FIELD_TYPE_KWARGS.get(actual_type, set())
    for key in allowed_extra:
        if key in field_config:
            kwargs[key] = field_config[key]

    if actual_type == "BooleanField" and "default" in kwargs:
        kwargs["default"] = bool(kwargs["default"])

    if actual_type == "CharField" and "max_length" not in kwargs:
        kwargs["max_length"] = 200

    if "verbose_name" not in kwargs:
        kwargs["verbose_name"] = name.replace('_', ' ').title()

    try:
        return field_class(**kwargs)
    except (TypeError, ValueError) as e:
        raise FieldError(f"字段 '{name}' 的参数无效: {e}")


def _build_fields_from_config(field_configs: List[Dict[str, Any]]) -> Dict[str, models.Field]:
    if not isinstance(field_configs, list):
        raise TypeError("field_configs 必须是列表")

    fields = {
        'id': models.AutoField(primary_key=True, verbose_name='主键ID'),
        'create_time': models.DateTimeField(auto_now_add=True, verbose_name='创建时间', editable=False),
        'update_time': models.DateTimeField(auto_now=True, verbose_name='更新时间', editable=False),
    }

    for idx, field in enumerate(field_configs):
        if not isinstance(field, dict):
            logger.warning(f"跳过无效字段配置（非字典）: {field}")
            continue

        name = field.get('name', '').strip()
        if not name or name in fields:
            continue

        try:
            fields[name] = _build_field(field)
        except Exception as e:
            logger.error(f"创建字段 '{name}' 失败: {e}", exc_info=True)
            continue

    return fields


def _generate_cache_key(model_name: str, table_name: str, fields_config: List[Dict[str, Any]]) -> str:
    normalized_fields = []
    for f in fields_config:
        if isinstance(f, dict) and f.get("name") and f.get("type"):
            allowed_keys = {"name", "type"} | COMMON_FIELD_KWARGS | FIELD_TYPE_KWARGS.get(f["type"], set())
            if f["type"] in ("ForeignKey", "foreignkey"):
                allowed_keys.update({"to", "on_delete", "null", "required", "options"})
            filtered = {k: v for k, v in f.items() if k in allowed_keys}
            normalized_fields.append(filtered)

    normalized_fields.sort(key=lambda x: x["name"])
    config_str = json.dumps(normalized_fields, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    raw_key = f"{model_name}|{table_name}|{config_str}"
    return hashlib.md5(raw_key.encode("utf-8")).hexdigest()


# ==================== 动态方法绑定（新增导出方法） ====================

def _get_method_lowcode_model():
    return apps.get_model('lowcode', 'MethodLowCode')


def _safe_get_user_role_ids(user):
    if user is None:
        return set()
    try:
        roles = getattr(user, 'roles', None)
        if roles is not None:
            return set(roles.values_list('id', flat=True))
        return set()
    except Exception:
        return set()


# ---------------- 新增：数据导出方法实现 ----------------
def _make_export_to_csv_method(model_class: Type[models.Model]) -> Callable:
    """生成带权限校验的CSV导出方法"""

    @record_method_call_log()
    def export_to_csv(self, user, file_path: str, fields: List[str] = None, encoding: str = 'utf-8') -> None:
        """
        导出模型数据到CSV文件（带权限校验、数据格式化）
        :param user: 操作用户（用于权限校验）
        :param file_path: 导出文件路径
        :param fields: 要导出的字段列表（None则导出所有非自动创建字段）
        :param encoding: 文件编码（默认utf-8，建议Excel使用gbk）
        """
        # 权限校验
        check_method_permission(user, model_class.__name__, 'export_to_csv')
        check_data_permission(user, self)

        # 确定导出字段
        export_fields = fields or [f.name for f in self._meta.fields if not f.auto_created]
        # 过滤无效字段
        valid_fields = [f for f in export_fields if hasattr(self, f)]
        if not valid_fields:
            raise ValueError("无有效导出字段")

        # 写入CSV
        with open(file_path, 'w', newline='', encoding=encoding) as f:
            writer = csv.writer(f)
            # 写入表头（使用verbose_name）
            headers = [self._meta.get_field(f).verbose_name or f for f in valid_fields]
            writer.writerow(headers)

            # 批量迭代查询，避免内存溢出
            for obj in self.objects.all().iterator():
                row = []
                for field in valid_fields:
                    value = getattr(obj, field, '')
                    # 特殊字段格式化
                    if isinstance(value, (datetime, date)):
                        value = value.strftime('%Y-%m-%d %H:%M:%S') if isinstance(value, datetime) else value.strftime(
                            '%Y-%m-%d')
                    elif isinstance(value, bool):
                        value = '是' if value else '否'
                    elif hasattr(value, '__str__'):
                        value = str(value)
                    row.append(value)
                writer.writerow(row)

        logger.info(
            f"✅ 用户 {user.username} 导出模型 {model_class.__name__} 数据成功，路径: {file_path}，字段数: {len(valid_fields)}")

    return export_to_csv


# ---------------- 原有方法工厂 ----------------
def _make_aggregate_method(method_name: str, params: dict, allowed_role_ids: Set[int]) -> Callable:
    @record_method_call_log()
    def method(self, user, *args, **kwargs):
        check_method_permission(user, self.__class__.__name__, method_name)
        check_data_permission(user, self)
        user_role_ids = _safe_get_user_role_ids(user)

        # 修正：使用Django标准的PermissionDenied替代PermissionError
        if allowed_role_ids and not (allowed_role_ids & user_role_ids):
            raise PermissionDenied(f"用户无权调用方法 {method_name}")

        related_manager = getattr(self, params["related_name"])
        agg_field = params["agg_field"]
        operation = params.get("operation", "sum").lower()
        multiply_field = params.get("multiply_field")
        expr = F(agg_field) * F(multiply_field) if multiply_field else F(agg_field)
        agg_ops = {
            "sum": Sum(expr),
            "avg": Avg(expr),
            "count": Count(expr),
            "max": Max(expr),
            "min": Min(expr),
        }
        op_func = agg_ops.get(operation)
        if not op_func:
            raise ValueError(f"不支持的聚合操作: {operation}")
        result = related_manager.all().aggregate(val=op_func)["val"]
        return result if result is not None else 0

    return method


def _make_field_update_method(method_name: str, params: dict, allowed_role_ids: Set[int]) -> Callable:
    @record_method_call_log()
    def method(self, user, new_value, *args, **kwargs):
        check_method_permission(user, self.__class__.__name__, method_name)
        check_data_permission(user, self)
        user_role_ids = _safe_get_user_role_ids(user)

        # 修正：使用Django标准的PermissionDenied替代PermissionError
        if allowed_role_ids and not (allowed_role_ids & user_role_ids):
            raise PermissionDenied(f"用户无权调用方法 {method_name}")

        field_name = params["field_name"]
        if new_value is not None:
            setattr(self, field_name, new_value)
            self.save(update_fields=[field_name])
        return getattr(self, field_name, None)

    return method


def _make_custom_func_method(method_name: str, params: dict, allowed_role_ids: Set[int]) -> Callable:
    @record_method_call_log()
    def method(self, user, *args, **kwargs):
        check_method_permission(user, self.__class__.__name__, method_name)
        check_data_permission(user, self)
        user_role_ids = _safe_get_user_role_ids(user)

        # 修正：使用Django标准的PermissionDenied替代PermissionError
        if allowed_role_ids and not (allowed_role_ids & user_role_ids):
            raise PermissionDenied(f"用户无权调用方法 {method_name}")

        func_path = params["func_path"]
        try:
            module_path, func_name = func_path.rsplit(".", 1)
            func = getattr(importlib.import_module(module_path), func_name)
        except (ValueError, ImportError, AttributeError) as e:
            raise RuntimeError(f"无法加载自定义函数 {func_path}: {e}")
        return func(self, *args, **kwargs)

    return method


METHOD_FACTORY_MAP = {
    "aggregate": _make_aggregate_method,
    "field_update": _make_field_update_method,
    "custom_func": _make_custom_func_method,
}


def _unbind_single_method(model_class: type, method_name: str) -> bool:
    internal_attr = f"{DYNAMIC_METHOD_PREFIX}{method_name}"
    removed = False
    if hasattr(model_class, internal_attr):
        delattr(model_class, internal_attr)
        removed = True
    if hasattr(model_class, method_name):
        try:
            delattr(model_class, method_name)
        except AttributeError:
            pass
    return removed


def bind_methods_to_model(model_class: type, force_rebind: bool = False) -> int:
    model_name = model_class.__name__

    # ---------------- 新增：绑定导出方法 ----------------
    export_method_key = (model_name, 'export_to_csv')
    if not force_rebind and export_method_key in _BOUND_DYNAMIC_METHODS:
        pass  # 已绑定，跳过
    else:
        if force_rebind:
            _unbind_single_method(model_class, 'export_to_csv')

        # 生成并绑定导出方法
        export_method = _make_export_to_csv_method(model_class)
        internal_attr = f"{DYNAMIC_METHOD_PREFIX}export_to_csv"
        setattr(model_class, internal_attr, export_method)

        # 创建代理方法
        def export_proxy(self, user, *args, **kwargs):
            real_method = getattr(self, internal_attr)
            return real_method(self, user, *args, **kwargs)

        setattr(model_class, 'export_to_csv', export_proxy)
        _BOUND_DYNAMIC_METHODS.add(export_method_key)
        logger.debug(f"✅ 绑定导出方法: {model_name}.export_to_csv")

    # ---------------- 原有方法绑定逻辑 ----------------
    try:
        MethodLowCode = _get_method_lowcode_model()
        configs = (
            MethodLowCode.objects
            .filter(model_name=model_name, is_active=True)
            .prefetch_related('roles')
        )
    except Exception as e:
        logger.warning(f"查询 MethodLowCode 失败（可能 DB 未初始化）: {e}")
        return 1  # 至少绑定了导出方法

    bound_count = 1  # 初始化为1（导出方法）
    for config in configs:
        method_name = config.method_name
        logic_type = config.logic_type
        params = config.params or {}
        key = (model_name, method_name)

        if not force_rebind and key in _BOUND_DYNAMIC_METHODS:
            continue

        if force_rebind:
            _unbind_single_method(model_class, method_name)

        factory = METHOD_FACTORY_MAP.get(logic_type)
        if not factory:
            logger.error(f"不支持的逻辑类型 '{logic_type}'，跳过方法 {model_name}.{method_name}")
            continue

        allowed_role_ids = set(config.roles.values_list('id', flat=True))

        try:
            internal_method = factory(method_name, params, allowed_role_ids)
            internal_attr = f"{DYNAMIC_METHOD_PREFIX}{method_name}"
            setattr(model_class, internal_attr, internal_method)

            def make_proxy(mn):
                def proxy(self, user, *args, _mn=mn, **kwargs):
                    real = getattr(self, f"{DYNAMIC_METHOD_PREFIX}{_mn}")
                    return real(self, user, *args, **kwargs)

                return proxy

            setattr(model_class, method_name, make_proxy(method_name))
            _BOUND_DYNAMIC_METHODS.add(key)
            bound_count += 1
            logger.debug(f"✅ 绑定方法: {model_name}.{method_name}")

        except Exception as e:
            logger.exception(f"❌ 绑定方法 {model_name}.{method_name} 失败: {e}")

    return bound_count


# ==================== 模型构建 ====================

def _build_model_class(
        model_name: str,
        table_name: str,
        fields_config: List[Dict[str, Any]]
) -> Type[models.Model]:
    cache_key = _generate_cache_key(model_name, table_name, fields_config)
    if cache_key in _DYNAMIC_MODEL_CACHE:
        logger.debug(f"[DEBUG] 命中缓存: 动态模型 '{model_name}' (表: {table_name})")
        return _DYNAMIC_MODEL_CACHE[cache_key]

    model_fields = _build_fields_from_config(fields_config)

    class DynamicModelMeta:
        db_table = table_name
        app_label = "lowcode"
        verbose_name = model_name
        verbose_name_plural = f"{model_name}列表"
        ordering = ["-create_time"]

    DynamicModel = type(
        model_name,
        (models.Model,),
        {
            **model_fields,
            "Meta": DynamicModelMeta,
            "__module__": "lowcode.models",
            "__str__": lambda self: f"{model_name}-{getattr(self, 'id', '未知ID')}",
        }
    )

    try:
        apps.register_model('lowcode', DynamicModel)
        # 手动创建数据库表
        with connection.schema_editor() as schema_editor:
            schema_editor.create_model(DynamicModel)
    except AppRegistryNotReady:
        logger.warning("App registry not ready; model registration may fail.")
        raise

    _DYNAMIC_MODEL_CACHE[cache_key] = DynamicModel
    logger.info(f"✅ 动态模型创建成功: {model_name} (table={table_name})")
    return DynamicModel


# ==================== 三大标准入口（保持兼容） ====================

def get_dynamic_model_by_id(model_id: int) -> Optional[Type[models.Model]]:
    try:
        obj = LowCodeModelConfig.objects.get(id=model_id)
        table_name = obj.table_name or f"lowcode_{obj.name.lower()}"
        model_class = _build_model_class(obj.name, table_name, obj.fields or [])
        bind_methods_to_model(model_class, force_rebind=False)
        return model_class
    except LowCodeModelConfig.DoesNotExist:
        logger.warning(f"[WARNING] LowCodeModelConfig ID={model_id} 不存在")
        return None
    except Exception as e:
        logger.error(f"[ERROR] 通过 ID={model_id} 构建模型失败: {e}", exc_info=True)
        return None


def get_dynamic_model_by_name(model_name: str) -> Optional[Type[models.Model]]:
    try:
        obj = LowCodeModelConfig.objects.get(name=model_name)
        table_name = obj.table_name or f"lowcode_{model_name.lower()}"
        model_class = _build_model_class(model_name, table_name, obj.fields or [])
        bind_methods_to_model(model_class, force_rebind=False)
        return model_class
    except LowCodeModelConfig.DoesNotExist:
        logger.debug(f"[DEBUG] 未找到模型 '{model_name}' 的配置")
        return None
    except Exception as e:
        logger.error(f"[ERROR] 构建模型 '{model_name}' 失败: {e}", exc_info=True)
        return None


def get_dynamic_model_by_config(
        model_name: str,
        fields: List[Dict[str, Any]],
        table_name: Optional[str] = None
) -> Type[models.Model]:
    if table_name is None:
        table_name = f"lowcode_{model_name.lower()}"
    model_class = _build_model_class(model_name, table_name, fields)
    return model_class


# ==================== 刷新与清理 ====================

def bind_methods_from_db(force_rebind: bool = False) -> None:
    """
    从数据库加载 MethodLowCode 配置，并将方法绑定到对应动态模型。

    :param force_rebind: 是否强制重新绑定已存在的方法（会先卸载旧版本）
    """
    with _BIND_LOCK:
        # 获取所有启用的配置
        active_configs = (
            MethodLowCode.objects
            .filter(is_active=True)
            .only("model_name", "method_name", "logic_type", "params")
        )

        # 按模型分组
        from collections import defaultdict
        model_config_map = defaultdict(list)
        for config in active_configs:
            model_config_map[config.model_name].append(config)

        bound_count = 0
        for model_name, configs in model_config_map.items():
            try:
                dynamic_model: type[models.Model] = apps.get_model("lowcode", model_name)
            except LookupError:
                logger.warning(f"跳过绑定：动态模型 '{model_name}' 未注册或不存在")
                continue

            # 先绑定导出方法（确保每个模型都有）
            export_key = (model_name, 'export_to_csv')
            if force_rebind or export_key not in _BOUND_DYNAMIC_METHODS:
                if force_rebind:
                    _unbind_single_method(dynamic_model, 'export_to_csv')
                export_method = _make_export_to_csv_method(dynamic_model)
                internal_attr = f"{DYNAMIC_METHOD_PREFIX}export_to_csv"
                setattr(dynamic_model, internal_attr, export_method)

                def export_proxy(self, user, *args, **kwargs):
                    real_method = getattr(self, internal_attr)
                    return real_method(self, user, *args, **kwargs)

                setattr(dynamic_model, 'export_to_csv', export_proxy)
                _BOUND_DYNAMIC_METHODS.add(export_key)
                logger.debug(f"✅ 重新绑定导出方法: {model_name}.export_to_csv")
                bound_count += 1

            for config in configs:
                method_name = config.method_name
                logic_type = config.logic_type
                params = config.params or {}
                full_name = f"{model_name}.{method_name}"

                # 如果非强制重绑，且已有公开方法（且是动态注入的），则跳过
                if not force_rebind and (model_name, method_name) in _BOUND_DYNAMIC_METHODS:
                    logger.debug(f"方法 {full_name} 已绑定，跳过")
                    continue

                # 若强制重绑，先卸载旧方法
                if force_rebind:
                    _unbind_single_method(dynamic_model, method_name)

                factory = METHOD_FACTORY_MAP.get(logic_type)
                if not factory:
                    logger.error(f"不支持的逻辑类型 '{logic_type}'，跳过方法 {full_name}")
                    continue

                try:
                    # 创建内部实现方法（带前缀）
                    internal_method = factory(method_name, params, set(config.roles.values_list('id', flat=True)))
                    internal_attr = f"{DYNAMIC_METHOD_PREFIX}{method_name}"
                    setattr(dynamic_model, internal_attr, internal_method)

                    # 创建公开代理方法（固化 method_name，避免闭包问题）
                    def make_proxy(proxy_method_name: str):
                        def proxy(self, user, *args, _mn=proxy_method_name, **kwargs):
                            real_method = getattr(self, f"{DYNAMIC_METHOD_PREFIX}{_mn}")
                            return real_method(self, user, *args, **kwargs)

                        return proxy

                    proxy_method = make_proxy(method_name)
                    setattr(dynamic_model, method_name, proxy_method)

                    # 记录绑定状态
                    _BOUND_DYNAMIC_METHODS.add((model_name, method_name))
                    bound_count += 1

                    logger.info(
                        f"✅ 成功绑定动态方法: {full_name} "
                        f"(type={logic_type}, params_keys={list(params.keys())})"
                    )

                except Exception as e:
                    logger.exception(f"❌ 绑定方法 {full_name} 失败: {e}")

        logger.info(f"共绑定 {bound_count} 个动态方法（含导出方法）")


def unbind_methods_from_db() -> None:
    with _BIND_LOCK:
        unbound = 0
        for model_name, method_name in list(_BOUND_DYNAMIC_METHODS):
            try:
                model_class = apps.get_model("lowcode", model_name)
                if _unbind_single_method(model_class, method_name):
                    unbound += 1
            except LookupError:
                pass
        _BOUND_DYNAMIC_METHODS.clear()
        logger.info(f"✅ 卸载 {unbound} 个动态方法（含导出方法）")


def refresh_dynamic_methods() -> None:
    """
    刷新所有动态方法：
    1. 卸载当前所有动态绑定的方法
    2. 重新从数据库加载并绑定启用的配置
    3. 确保导出方法重新绑定
    """
    with _BIND_LOCK:
        logger.info("🔄 开始刷新动态方法...")
        unbind_methods_from_db()
        bind_methods_from_db(force_rebind=True)
        logger.info("✅ 动态方法刷新完成（含导出方法）。")


def refresh_dynamic_model(model_name: str) -> bool:
    """
    从数据库重新加载指定名称的低代码模型定义，并注册其动态模型类与方法。

    Args:
        model_name: 动态模型的逻辑名称（对应 LowCodeModelConfig.name）

    Returns:
        bool: True 表示模型成功加载并注册；False 表示失败（如模型不存在或配置错误）
    """
    # 输入校验
    if not model_name or not isinstance(model_name, str):
        logger.warning(f"⚠️ 无效模型名类型或为空: {repr(model_name)}")
        return False

    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', model_name):
        logger.warning(f"⚠️ 模型名 '{model_name}' 不符合 Python 标识符规范")
        return False

    try:
        logger.debug(f"🔄 刷新动态模型: {model_name}")
        model_class = get_dynamic_model_with_methods(model_name)
        if model_class is not None:
            # 确保导出方法已绑定
            if not hasattr(model_class, 'export_to_csv'):
                bind_methods_to_model(model_class, force_rebind=True)
            logger.info(f"✅ 动态模型刷新成功: {model_name}（含导出方法）")
            return True
        else:
            logger.warning(f"⚠️ 未能加载动态模型（返回 None）: {model_name}")
            return False
    except (ObjectDoesNotExist, ValueError, KeyError) as e:
        logger.error(f"❌ 刷新动态模型失败（业务错误）: {model_name} - {e}")
        return False
    except Exception as e:
        logger.exception(f"🔥 刷新动态模型时发生未预期错误: {model_name} - {e}")
        return False


def cleanup_dynamic_models() -> None:
    global _DYNAMIC_MODEL_CACHE, _DYNAMIC_MODEL_REGISTRY, _DYNAMIC_MODELS_LOADED
    unbind_methods_from_db()
    _DYNAMIC_MODEL_CACHE.clear()
    _DYNAMIC_MODEL_REGISTRY.clear()
    _DYNAMIC_MODELS_LOADED = False
    logger.info("🧹 动态模型缓存和注册表已清理（含导出方法）")


# ==================== 辅助函数 ====================

def is_model_name_unique(name: str, exclude_id: int = None) -> bool:
    qs = LowCodeModelConfig.objects.filter(name=name)
    if exclude_id is not None:
        qs = qs.exclude(id=exclude_id)
    return not qs.exists()


def is_table_name_unique(table_name: str, exclude_id: int = None) -> bool:
    qs = LowCodeModelConfig.objects.filter(table_name=table_name)
    if exclude_id is not None:
        qs = qs.exclude(id=exclude_id)
    return not qs.exists()


def ensure_unique_table_name(base_name: str, exclude_id: int = None) -> str:
    table_name = base_name
    suffix = 1
    while not is_table_name_unique(table_name, exclude_id):
        table_name = f"{base_name}_{suffix}"
        suffix += 1
    return table_name


def verify_dynamic_tables() -> None:
    existing_tables = {t.lower() for t in connection.introspection.table_names()}
    verified_count = 0

    for model in apps.get_models(include_auto_created=True):
        if model._meta.app_label == 'lowcode' and model.__module__ == 'lowcode.models':
            if hasattr(model, '_meta') and model._meta.managed is False:
                continue
            table_name = model._meta.db_table.lower()
            if table_name not in existing_tables:
                logger.warning(
                    f"⚠️  模型 {model.__name__} 对应的数据表 {table_name} 不存在！"
                )
            else:
                verified_count += 1

    logger.debug(f"✅ 完成数据表验证，共检查 {verified_count} 个动态模型表。")


def create_table_for_dynamic_model(model_class: Type[models.Model]) -> bool:
    """
    为动态模型创建数据库表（仅当表不存在时）
    """
    table_name = model_class._meta.db_table
    with connection.cursor() as cursor:
        # 检查表是否存在
        if table_name in [t.lower() for t in connection.introspection.table_names()]:
            logger.debug(f"📊 表 '{table_name}' 已存在，跳过创建")
            return True

        # 生成 CREATE TABLE 语句
        style = no_style()
        sql, params = connection.ops.sql_create_model(model_class, style)
        if not sql:
            logger.warning(f"⚠️ 无法为模型 {model_class.__name__} 生成 CREATE TABLE 语句")
            return False

        try:
            for statement in sql:
                cursor.execute(statement, params)
            logger.info(f"✅ 成功创建数据库表: {table_name}")
            return True
        except Exception as e:
            logger.error(f"❌ 创建表 '{table_name}' 失败: {e}", exc_info=True)
            return False


# ==================== 兼容旧接口（修正重复定义问题） ====================
get_dynamic_model_with_methods = get_dynamic_model_by_name
create_dynamic_model = get_dynamic_model_by_config
get_or_create_dynamic_model = get_dynamic_model_by_config