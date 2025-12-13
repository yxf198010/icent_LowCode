"""
动态模型注册中心：负责从 JSON 创建模型类，并注册到 Django。
支持线程安全、字段扩展、自动模块路径、模型名校验等。
"""
# 问题：Django 无法对 managed=True 的动态模型生成 migrations
# 因为：
#
# migrations 是在 启动时静态扫描 models.py 生成的；
# 动态 type(..., (models.Model,), ...) 创建的类 不会被 makemigrations 发现；
# 所以即使设为 managed=True，Django 也不会自动建表，除非你手动执行 SQL 或用其他方式创建表。
# 结论：
# 如果你真的希望 Django 管理表（即自动建表），那你必须：
# 将模型写入真实的 .py 文件（如 generated_models.py），然后触发 makemigrations；
# 或者在注册后手动调用 schema_editor.create_model() 来建表（不推荐，绕过 migration 系统）。
# 否则，即使 managed=True，表也不会自动创建，导致运行时报错“table not found”。

# # 在 views.py 的 create_model_api 中调用
# from .dynamic_model_registry import (
#     _create_dynamic_model,
#     _DYNAMIC_MODEL_REGISTRY,
#     create_dynamic_model_table
# )
#
# # 1. 动态创建模型类并注册
# model_class = _create_dynamic_model(model_name, fields_config)
# _DYNAMIC_MODEL_REGISTRY[model_name] = model_class
#
# # 2. 创建数据表
# create_dynamic_model_table(model_name)

import json
import re
import threading
from pathlib import Path
from types import ModuleType
from django.apps import apps
from django.conf import settings
from django.db import models
from django.core.exceptions import ValidationError
import logging
from django.db import connection
from django.db.utils import ProgrammingError, OperationalError
import uuid  # 需在文件顶部导入uuid模块

logger = logging.getLogger('lowcode')

# 线程锁：确保 initialize/cleanup 是原子操作
_REGISTRY_LOCK = threading.Lock()

# 全局注册表：存储已创建的动态模型类
_DYNAMIC_MODEL_REGISTRY = {}  # {model_name: ModelClass}

# 默认配置文件路径（可被 settings 覆盖）
_DEFAULT_CONFIG_PATH = Path(settings.BASE_DIR) / 'dynamic_models.json'
DYNAMIC_MODEL_CONFIG_PATH = getattr(settings, 'DYNAMIC_MODEL_CONFIG_PATH', _DEFAULT_CONFIG_PATH)


# 支持的字段类型映射（扩展更多常用字段）
SUPPORTED_FIELD_TYPES = {
    # 基础字段
    'CharField': models.CharField,
    'TextField': models.TextField,
    'IntegerField': models.IntegerField,
    'BigIntegerField': models.BigIntegerField,
    'SmallIntegerField': models.SmallIntegerField,
    'BooleanField': models.BooleanField,
    'DateField': models.DateField,
    'DateTimeField': models.DateTimeField,
    'EmailField': models.EmailField,
    'DecimalField': models.DecimalField,
    'FloatField': models.FloatField,
    'URLField': models.URLField,
    'UUIDField': models.UUIDField,
    'JSONField': models.JSONField,  # Django >= 3.1
    # 扩展字段
    'FileField': models.FileField,
    'ImageField': models.ImageField,
    'TimeField': models.TimeField,
    'PositiveIntegerField': models.PositiveIntegerField,
    'PositiveSmallIntegerField': models.PositiveSmallIntegerField,
}

# 字段默认参数（避免必填参数缺失）
FIELD_DEFAULT_OPTIONS = {
    "CharField": {"max_length": 255},
    "TextField": {},
    "IntegerField": {},
    "BigIntegerField": {},
    "SmallIntegerField": {},
    "PositiveIntegerField": {},
    "PositiveSmallIntegerField": {},
    "BooleanField": {"default": False},
    "DateField": {},
    "DateTimeField": {},
    "TimeField": {},
    "EmailField": {"max_length": 254},
    "URLField": {"max_length": 200},
    "DecimalField": {"max_digits": 10, "decimal_places": 2},
    "FloatField": {},
    # ✅ 修复UUIDField默认值
    "UUIDField": {"default": uuid.uuid4},
    "JSONField": {},
    "FileField": {"upload_to": "lowcode/files/"},
    "ImageField": {"upload_to": "lowcode/images/"},
    "ForeignKey": {"on_delete": models.CASCADE},
}


def _is_valid_model_name(name: str) -> bool:
    """校验模型名是否为合法的 Python 标识符"""
    return bool(re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name))


def _resolve_foreign_key_target(to_model: str) -> str:
    """
    解析 ForeignKey 目标模型：
    - 若未指定 app_label（如 'User'），默认补全为 'lowcode.User'
    - 若已指定（如 'auth.User'），保持不变
    """
    if '.' not in to_model:
        return f'lowcode.{to_model}'
    return to_model


def _get_field_default_options(field_type: str) -> dict:
    """获取字段类型的默认参数（避免必填项缺失）"""
    return FIELD_DEFAULT_OPTIONS.get(field_type, {})


def _create_dynamic_model(model_name: str, fields_config: dict) -> type:
    """根据字段配置动态创建 Django 模型类"""
    if not _is_valid_model_name(model_name):
        raise ValueError(f"非法模型名 '{model_name}'：必须是合法的 Python 标识符")

    # 自动推导 __module__（更健壮）
    caller_module = 'lowcode.models'
    try:
        # 尝试获取当前 app 的 models 模块
        app_config = apps.get_app_config('lowcode')
        if hasattr(app_config, 'models_module') and app_config.models_module:
            caller_module = app_config.models_module.__name__
    except LookupError:
        pass  # fallback to 'lowcode.models'

    # 模型元信息配置
    meta_attrs = {
        'app_label': 'lowcode',
        'managed': True,  # 标记为Django管理，但需手动建表
        'db_table': f'lowcode_{model_name.lower()}',
        'verbose_name': model_name,
        'verbose_name_plural': f'{model_name}列表',
    }

    attrs = {
        '__module__': caller_module,
        'Meta': type('Meta', (), meta_attrs),
        # 默认添加主键（若未配置）
        'id': models.BigAutoField(primary_key=True, verbose_name='ID'),
    }

    # 遍历字段配置创建字段
    for field_name, field_spec in fields_config.items():
        if not _is_valid_model_name(field_name):
            raise ValueError(f"非法字段名 '{field_name}' in model '{model_name}'")

        field_type = field_spec.get('type')
        options = field_spec.get('options', {})

        # 合并默认参数和自定义参数（自定义参数优先级更高）
        field_options = {**_get_field_default_options(field_type), **options}

        try:
            if field_type == 'ForeignKey':
                # 处理外键字段
                to_model = field_spec.get('to')
                if not to_model:
                    raise ValueError(f"ForeignKey '{field_name}' 缺少 'to' 参数")
                resolved_to = _resolve_foreign_key_target(to_model)
                # 外键默认级联删除
                field_options.setdefault('on_delete', models.CASCADE)
                attrs[field_name] = models.ForeignKey(resolved_to, **field_options)

            elif field_type in SUPPORTED_FIELD_TYPES:
                # 处理普通字段
                field_class = SUPPORTED_FIELD_TYPES[field_type]
                attrs[field_name] = field_class(**field_options)

            else:
                raise ValueError(f"不支持的字段类型: {field_type}（支持: {list(SUPPORTED_FIELD_TYPES.keys())}）")

        except Exception as e:
            raise ValueError(f"创建字段 '{field_name}' 失败: {str(e)}") from e

    # 动态创建模型类
    model_class = type(model_name, (models.Model,), attrs)
    logger.debug(f"[DEBUG] 动态创建模型类: {model_name} (表名: {meta_attrs['db_table']})")
    return model_class


def load_model_config() -> dict:
    """从文件加载动态模型配置"""
    config_path = DYNAMIC_MODEL_CONFIG_PATH
    if not config_path.exists():
        logger.debug(f"[DEBUG] 动态模型配置文件不存在: {config_path}")
        return {}

    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
            if not isinstance(config, dict):
                raise ValueError("配置文件根节点必须是 JSON 对象")
            return config
    except Exception as e:
        logger.error(f"[ERROR] 加载动态模型配置失败 ({config_path}): {e}", exc_info=True)
        return {}


def save_model_config(config: dict):
    """保存动态模型配置到文件"""
    if not isinstance(config, dict):
        raise TypeError("配置必须是字典")

    config_path = DYNAMIC_MODEL_CONFIG_PATH
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        logger.info(f"[OK] 动态模型配置已保存至: {config_path}")
    except Exception as e:
        logger.error(f"[ERROR] 保存动态模型配置失败 ({config_path}): {e}", exc_info=True)
        raise


def initialize_dynamic_models():
    """
    初始化所有动态模型（线程安全）：
    1. 从 JSON 加载配置
    2. 动态创建模型类
    3. 注册到 Django AppRegistry
    """
    global _DYNAMIC_MODEL_REGISTRY

    with _REGISTRY_LOCK:
        config = load_model_config()
        if not config:
            logger.info("[OK] 无动态模型配置，跳过初始化")
            return

        app_config = apps.get_app_config('lowcode')
        new_models = 0

        for model_name, model_spec in config.items():
            if model_name in _DYNAMIC_MODEL_REGISTRY:
                logger.debug(f"[DEBUG] 模型 {model_name} 已存在，跳过")
                continue

            if 'fields' not in model_spec:
                logger.warning(f"[WARNING] 模型 {model_name} 缺少 'fields' 字段，跳过")
                continue

            try:
                model_class = _create_dynamic_model(model_name, model_spec['fields'])
                _DYNAMIC_MODEL_REGISTRY[model_name] = model_class

                # 注册到 Django AppRegistry（使用小写 key，Django 惯例）
                model_key = model_name.lower()
                if model_key not in app_config.models:
                    app_config.models[model_key] = model_class
                    logger.debug(f"[DEBUG] 模型 {model_name} 已注册到 Django AppRegistry")
                    new_models += 1
                else:
                    logger.debug(f"[DEBUG] 模型 {model_name} 已存在于 AppRegistry，跳过注册")

            except Exception as e:
                logger.error(f"[ERROR] 创建动态模型 {model_name} 失败: {e}", exc_info=True)
                raise

        logger.info(f"[OK] 成功初始化 {new_models} 个新动态模型（共 {len(_DYNAMIC_MODEL_REGISTRY)} 个）")


def cleanup_dynamic_models():
    """
    清理所有动态模型（线程安全）：
    - 从全局注册表移除
    - 从 Django AppRegistry 移除
    - 清除 ContentType 缓存
    """
    global _DYNAMIC_MODEL_REGISTRY

    with _REGISTRY_LOCK:
        if not _DYNAMIC_MODEL_REGISTRY:
            logger.debug("[DEBUG] 动态模型注册表为空，无需清理")
            return

        app_config = apps.get_app_config('lowcode')
        removed_count = 0

        for model_name in list(_DYNAMIC_MODEL_REGISTRY.keys()):
            model_key = model_name.lower()
            if model_key in app_config.models:
                del app_config.models[model_key]
                logger.debug(f"[DEBUG] 从 AppRegistry 移除模型: {model_name}")
                removed_count += 1

        _DYNAMIC_MODEL_REGISTRY.clear()

        # 清除 ContentType 缓存（关键！防止 admin 或 ORM 使用旧模型）
        try:
            from django.contrib.contenttypes.models import ContentType
            ContentType.objects.clear_cache()
        except Exception:
            # 如果 contenttypes 未安装，忽略
            pass

        logger.info(f"[OK] 成功清理 {removed_count} 个动态模型")


def get_dynamic_model(model_name: str) -> type | None:
    """安全获取已注册的动态模型类"""
    return _DYNAMIC_MODEL_REGISTRY.get(model_name)


def table_exists(table_name: str) -> bool:
    """
    通用表存在性校验（兼容多数据库）
    :param table_name: 数据表名（如 lowcode_product）
    :return: 是否存在
    """
    try:
        with connection.cursor() as cursor:
            vendor = connection.vendor
            if vendor == 'postgresql':
                # PostgreSQL 校验
                cursor.execute("SELECT to_regclass(%s)", [table_name])
                return cursor.fetchone()[0] is not None
            elif vendor in ('mysql', 'mariadb'):
                # MySQL/MariaDB 校验
                db_name = connection.settings_dict['NAME']
                cursor.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_name = %s",
                    [db_name, table_name]
                )
                return cursor.fetchone() is not None
            else:
                # SQLite 及其他数据库
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=%s;", [table_name])
                return cursor.fetchone() is not None
    except (ProgrammingError, OperationalError) as e:
        logger.error(f"[ERROR] 校验表 {table_name} 存在性失败: {e}", exc_info=True)
        return False


def create_dynamic_model_table(model_name: str) -> bool:
    """
    为单个动态模型创建数据库表（核心新增方法）
    :param model_name: 模型名称（如 Product）
    :return: 是否创建成功
    """
    with _REGISTRY_LOCK:
        # 1. 校验模型是否已注册
        model_class = get_dynamic_model(model_name)
        if not model_class:
            logger.error(f"[ERROR] 模型 {model_name} 未注册，无法创建表")
            return False

        # 2. 获取表名并校验是否已存在
        table_name = model_class._meta.db_table
        if table_exists(table_name):
            logger.info(f"[INFO] 表 {table_name} 已存在，跳过创建")
            return True

        # 3. 使用 schema_editor 创建表
        try:
            with connection.schema_editor() as schema_editor:
                schema_editor.create_model(model_class)
                logger.info(f"[OK] 成功创建表: {table_name} (模型: {model_name})")
                return True
        except Exception as e:
            logger.error(f"[ERROR] 创建表 {table_name} 失败 (模型: {model_name}): {e}", exc_info=True)
            return False


def create_dynamic_model_tables():
    """为所有已注册的动态模型创建数据库表（批量建表，兼容原有逻辑）"""
    with _REGISTRY_LOCK:
        if not _DYNAMIC_MODEL_REGISTRY:
            logger.info("[INFO] 无已注册的动态模型，跳过批量建表")
            return

        created_count = 0
        with connection.schema_editor() as schema_editor:
            for model_name, model_class in _DYNAMIC_MODEL_REGISTRY.items():
                table_name = model_class._meta.db_table
                if table_exists(table_name):
                    logger.debug(f"[DEBUG] 表 {table_name} 已存在，跳过")
                    continue

                try:
                    schema_editor.create_model(model_class)
                    logger.info(f"[OK] 批量创建表: {table_name} (模型: {model_name})")
                    created_count += 1
                except Exception as e:
                    logger.error(f"[ERROR] 批量创建表 {table_name} 失败 (模型: {model_name}): {e}", exc_info=True)

        logger.info(f"[OK] 批量建表完成，共创建 {created_count} 个表（总计 {len(_DYNAMIC_MODEL_REGISTRY)} 个模型）")


def delete_dynamic_model_table(model_name: str) -> bool:
    """
    为单个动态模型删除数据库表（可选扩展）
    :param model_name: 模型名称
    :return: 是否删除成功
    """
    with _REGISTRY_LOCK:
        model_class = get_dynamic_model(model_name)
        if not model_class:
            logger.error(f"[ERROR] 模型 {model_name} 未注册，无法删除表")
            return False

        table_name = model_class._meta.db_table
        if not table_exists(table_name):
            logger.info(f"[INFO] 表 {table_name} 不存在，跳过删除")
            return True

        try:
            with connection.schema_editor() as schema_editor:
                schema_editor.delete_model(model_class)
                logger.info(f"[OK] 成功删除表: {table_name} (模型: {model_name})")
                return True
        except Exception as e:
            logger.error(f"[ERROR] 删除表 {table_name} 失败 (模型: {model_name}): {e}", exc_info=True)
            return False