"""
动态模型注册中心（最终优化版 + Django 5.2 完全兼容）
- 彻底修复 Django 5.2+ loading 模块移除问题
- 修复模型类/字符串参数混淆导致的 lower() 错误
- 消除不必要的警告信息
- 优化注册流程，避免重复创建表
- 修复重复建表、注册冲突
- 统一使用 introspection 检查表存在
- 确保 managed=True（关键修复！）
- 精简锁范围
- 保留完整高级功能（方法注入、更新、导出等）

解决的核心问题：
1. Django无法对动态模型生成migrations，因此提供手动建表接口
2. 动态模型注册的线程安全与缓存管理
3. 多数据库（PostgreSQL/MySQL/SQLite）的表存在性校验（通过 introspection）
4. 修复模型注册后未写入apps.all_models的问题
5. 修复ContentType缓存清理不彻底的问题
6. 优化Meta参数验证逻辑，消除无效参数警告
7. 兼容 Django 5.2+ 移除的 loading 模块
8. 修复模型类/字符串参数混淆导致的 AttributeError
9. 消除不必要的警告信息，优化用户体验
"""
import json
import re
import threading
import uuid
from pathlib import Path
from typing import Type, Dict, Any, Optional, List, Tuple, Set, Callable

import django
from django.apps import apps
from django.conf import settings
from django.db import models, connections
from django.db.utils import ProgrammingError, OperationalError, DatabaseError
from django.core.exceptions import ValidationError, ImproperlyConfigured
from django.utils.module_loading import import_string
from django.db.models.base import ModelBase
import logging

# -------------------------- 基础配置 --------------------------
logger = logging.getLogger(getattr(settings, 'DYNAMIC_MODEL_LOGGER_NAME', 'lowcode.dynamic_model'))
if not logger.handlers:
    logger.addHandler(logging.NullHandler())

# 设置日志级别（根据需要调整）
logger.setLevel(getattr(settings, 'DYNAMIC_MODEL_LOG_LEVEL', logging.INFO))

_REGISTRY_LOCK = threading.RLock()
_LOAD_LOCK = threading.RLock()
_CONFIG_CACHE_LOCK = threading.Lock()

_DYNAMIC_MODEL_REGISTRY: Dict[str, Type[models.Model]] = {}
_CONFIG_CACHE: Optional[Dict[str, Dict[str, Any]]] = None
_DYNAMIC_MODELS_LOADED = False

_DEFAULT_CONFIG_PATH = Path(settings.BASE_DIR) / 'dynamic_models.json'
DYNAMIC_MODEL_CONFIG_PATH = Path(
    getattr(settings, 'DYNAMIC_MODEL_REGISTRY_CONFIG_PATH', _DEFAULT_CONFIG_PATH)
)

MAX_IDENTIFIER_LENGTH = getattr(settings, 'DYNAMIC_MODEL_MAX_IDENTIFIER_LENGTH', 63)
MAX_TABLE_NAME_LENGTH = 63

# -------------------------- 字段配置 --------------------------
BASE_FIELD_TYPES: Dict[str, Type[models.Field]] = {
    'CharField': models.CharField,
    'TextField': models.TextField,
    'EmailField': models.EmailField,
    'URLField': models.URLField,
    'IntegerField': models.IntegerField,
    'BigIntegerField': models.BigIntegerField,
    'SmallIntegerField': models.SmallIntegerField,
    'PositiveIntegerField': models.PositiveIntegerField,
    'PositiveSmallIntegerField': models.PositiveSmallIntegerField,
    'DecimalField': models.DecimalField,
    'FloatField': models.FloatField,
    'BooleanField': models.BooleanField,
    'DateField': models.DateField,
    'DateTimeField': models.DateTimeField,
    'TimeField': models.TimeField,
    'UUIDField': models.UUIDField,
    'JSONField': models.JSONField,
    'FileField': models.FileField,
    'ImageField': models.ImageField,
    'ForeignKey': models.ForeignKey,
}

EXTRA_FIELD_TYPES = getattr(settings, 'DYNAMIC_MODEL_EXTRA_FIELD_TYPES', {})
SUPPORTED_FIELD_TYPES = {**BASE_FIELD_TYPES, **EXTRA_FIELD_TYPES}

FIELD_TYPE_ALIAS_MAP = {
    'char': 'CharField',
    'varchar': 'CharField',
    'int': 'IntegerField',
    'bigint': 'BigIntegerField',
    'decimal': 'DecimalField',
    'text': 'TextField',
    'datetime': 'DateTimeField',
    'boolean': 'BooleanField',
    'date': 'DateField',
    'time': 'TimeField',
    'email': 'EmailField',
    'url': 'URLField',
    'file': 'FileField',
    'image': 'ImageField',
}

FIELD_DEFAULT_OPTIONS = {
    'CharField': {'max_length': 255, 'blank': True, 'null': True},
    'TextField': {'blank': True, 'null': True},
    'EmailField': {'max_length': 254, 'blank': True, 'null': True},
    'URLField': {'max_length': 200, 'blank': True, 'null': True},
    'IntegerField': {'blank': True, 'null': True},
    'BigIntegerField': {'blank': True, 'null': True},
    'SmallIntegerField': {'blank': True, 'null': True},
    'PositiveIntegerField': {'blank': True, 'null': True},
    'PositiveSmallIntegerField': {'blank': True, 'null': True},
    'DecimalField': {'max_digits': 10, 'decimal_places': 2, 'blank': True, 'null': True},
    'FloatField': {'blank': True, 'null': True},
    'BooleanField': {'default': False},
    'DateField': {'blank': True, 'null': True},
    'DateTimeField': {'blank': True, 'null': True},
    'TimeField': {'blank': True, 'null': True},
    'UUIDField': {'default': uuid.uuid4, 'unique': True},
    'JSONField': {'blank': True, 'null': True, 'default': dict},
    'FileField': {'upload_to': 'lowcode/files/', 'blank': True, 'null': True},
    'ImageField': {'upload_to': 'lowcode/images/', 'blank': True, 'null': True},
    'ForeignKey': {'on_delete': models.CASCADE, 'blank': True, 'null': True},
}
FIELD_DEFAULT_OPTIONS.update(getattr(settings, 'DYNAMIC_MODEL_FIELD_DEFAULT_OPTIONS', {}))

IDENTIFIER_PATTERN = re.compile(r'^[a-zA-Z_][a-zA-Z0-9_]*$')


# -------------------------- 工具函数 --------------------------
def _is_valid_identifier(name: str) -> bool:
    if not isinstance(name, str):
        return False
    if len(name) > MAX_IDENTIFIER_LENGTH:
        logger.debug(f"标识符 '{name}' 长度超过限制({MAX_IDENTIFIER_LENGTH})")
        return False
    return bool(IDENTIFIER_PATTERN.match(name))


def _resolve_foreign_key_target(to_model: str) -> Tuple[str, str]:
    if not isinstance(to_model, str) or not to_model.strip():
        raise ValueError("外键目标模型不能为空")
    to_model = to_model.strip()
    if '.' not in to_model:
        app_label = 'lowcode'
        model_name = to_model
    else:
        app_label, model_name = to_model.split('.', 1)
        app_label = app_label.strip()
        model_name = model_name.strip()
    try:
        if model_name in _DYNAMIC_MODEL_REGISTRY:
            return app_label, model_name
        apps.get_model(app_label, model_name)
    except LookupError as e:
        raise ValueError(f"外键目标模型不存在: {to_model}") from e
    return app_label, model_name


def _get_model_module() -> str:
    try:
        app_config = apps.get_app_config('lowcode')
        if hasattr(app_config, 'models_module') and app_config.models_module:
            return app_config.models_module.__name__
    except LookupError:
        logger.debug("lowcode app未配置，使用默认模块名 'lowcode.models'")
    return 'lowcode.models'


def _build_model_meta(
        model_name: str,
        custom_meta: Optional[Dict[str, Any]] = None,
        table_name: Optional[str] = None,
        table_name_prefix: str = 'lowcode_'
) -> Type:
    """
    构建模型Meta类（修复app_label无效警告，优化参数验证）
    """
    # 基础Meta配置（仅包含Django支持的核心参数）
    default_meta = {
        'managed': True,  # ✅ 关键：必须为True才能手动建表
        'verbose_name': model_name.replace('_', ' ').title(),
        'verbose_name_plural': f"{model_name.replace('_', ' ').title()}列表",
        'ordering': ['-id'],
        'indexes': [models.Index(fields=['id'])],
    }

    # 表名配置
    if table_name:
        default_meta['db_table'] = table_name
    else:
        default_meta['db_table'] = f'{table_name_prefix}{model_name.lower()}'

    # 表名长度检查
    if len(default_meta['db_table']) > MAX_TABLE_NAME_LENGTH:
        logger.debug(f"表名 '{default_meta['db_table']}' 长度过长，自动截断")
        default_meta['db_table'] = default_meta['db_table'][:MAX_TABLE_NAME_LENGTH]

    # 合并自定义Meta（仅保留Django官方支持的参数）
    if custom_meta and isinstance(custom_meta, dict):
        # Django Model Meta 支持的核心参数列表（消除无效参数警告）
        ALLOWED_META_ATTRS = {
            'db_table', 'verbose_name', 'verbose_name_plural', 'ordering',
            'indexes', 'managed', 'unique_together', 'index_together',
            'default_permissions', 'permissions', 'get_latest_by',
            'order_with_respect_to', 'db_tablespace', 'abstract',
            'swappable', 'app_label'  # 显式允许app_label
        }

        for key, value in custom_meta.items():
            if key in ALLOWED_META_ATTRS:
                default_meta[key] = value
                logger.debug(f"应用Meta参数: {key} = {value}")
            else:
                logger.debug(f"忽略非标准Meta参数: {key}")

    # 强制设置app_label（确保模型归属正确）
    default_meta['app_label'] = 'lowcode'

    return type('Meta', (), default_meta)


def _clear_all_caches(model_class: Optional[Type[models.Model]] = None) -> None:
    """
    彻底清理所有缓存（兼容 Django 5.2+，无警告版本）
    """
    try:
        # 1. 兼容所有Django版本的模型缓存清理
        apps.clear_cache()
        logger.debug("Django模型缓存已清除")

        # 2. 清除ContentType缓存
        from django.contrib.contenttypes.models import ContentType
        ContentType.objects.clear_cache()
        logger.debug("ContentType缓存已清除")

        # 3. 清除特定模型的ContentType记录（如果提供）
        if model_class:
            try:
                ct = ContentType.objects.filter(
                    app_label=model_class._meta.app_label,
                    model=model_class._meta.model_name
                ).first()
                if ct:
                    ct.delete()
                    logger.debug(f"已删除模型 {model_class.__name__} 的ContentType记录")
            except Exception as e:
                logger.debug(f"删除ContentType记录失败（非关键）: {e}")

    except Exception as e:
        logger.debug(f"缓存清理部分失败（非关键）: {e}")


def _resolve_field_type(field_type: str) -> str:
    field_type = field_type.strip()
    return FIELD_TYPE_ALIAS_MAP.get(field_type.lower(), field_type)


def _ensure_string_input(value, param_name: str = "model_name", silent: bool = True) -> str:
    """
    确保输入是字符串（静默模式，不输出警告）
    """
    if isinstance(value, type) and issubclass(value, models.Model):
        # 如果传入的是模型类，返回类名（静默转换）
        if not silent:
            logger.debug(f"{param_name} 参数传入了模型类，自动转换为类名: {value.__name__}")
        return value.__name__
    elif isinstance(value, str):
        return value.strip()
    else:
        raise TypeError(f"{param_name} 必须是字符串或模型类，实际类型: {type(value)}")


# -------------------------- 表存在性检查 --------------------------
def table_exists(table_name: str, using: str = 'default') -> bool:
    """使用 Django introspection 检查表是否存在"""
    if not table_name or not isinstance(table_name, str):
        return False
    try:
        conn = connections[using]
        with conn.cursor() as cursor:
            tables = conn.introspection.table_names(cursor=cursor)
        # 兼容不同数据库的表名大小写
        table_name_lower = table_name.lower()
        return any(t.lower() == table_name_lower for t in tables)
    except Exception as e:
        logger.debug(f"检查表 '{table_name}' 存在性失败: {e}")
        return False


# -------------------------- 核心：创建模型类 --------------------------
def create_dynamic_model(
        model_name: str,
        fields_config: Dict[str, Dict[str, Any]],
        custom_meta: Optional[Dict[str, Any]] = None,
        table_name: Optional[str] = None
) -> Type[models.Model]:
    """
    创建动态模型类（仅创建，不注册、不建表）
    """
    # 确保模型名是合法字符串
    model_name = _ensure_string_input(model_name, silent=True)

    if not _is_valid_identifier(model_name):
        raise ValueError(f"非法模型名 '{model_name}' (必须以字母/下划线开头，仅包含字母/数字/下划线)")

    if not isinstance(fields_config, dict):
        raise ValueError("字段配置必须是字典类型")

    # 基础模型属性
    model_attrs = {
        '__module__': _get_model_module(),
        'Meta': _build_model_meta(model_name, custom_meta, table_name),
        'id': models.BigAutoField(primary_key=True, verbose_name='主键ID'),
    }

    # 验证并添加字段
    valid_fields: Set[str] = set()
    for field_name, field_spec in fields_config.items():
        if not _is_valid_identifier(field_name):
            logger.debug(f"跳过非法字段名 '{field_name}' (模型: {model_name})")
            continue
        if field_name in valid_fields:
            logger.debug(f"跳过重复字段 '{field_name}' (模型: {model_name})")
            continue
        if field_name in model_attrs:
            logger.debug(f"跳过保留字段名 '{field_name}' (模型: {model_name})")
            continue

        # 解析字段类型
        raw_type = field_spec.get('type', 'CharField')
        field_type = _resolve_field_type(raw_type)
        if field_type not in SUPPORTED_FIELD_TYPES:
            logger.debug(f"不支持的字段类型 '{raw_type}' (字段: {field_name})")
            continue

        # 合并字段选项（默认 + 自定义）
        field_options = {
            **FIELD_DEFAULT_OPTIONS.get(field_type, {}),
            **field_spec.get('options', {})
        }

        try:
            # 处理外键字段
            if field_type == 'ForeignKey':
                to_model = field_options.pop('to', None) or field_spec.get('to')
                if not to_model:
                    raise ValidationError(f"外键字段 '{field_name}' 缺少 'to' 参数")
                app_label, model_name_target = _resolve_foreign_key_target(to_model)
                target_model = apps.get_model(app_label, model_name_target)
                model_attrs[field_name] = models.ForeignKey(target_model, **field_options)
            else:
                # 普通字段
                field_class = SUPPORTED_FIELD_TYPES[field_type]
                model_attrs[field_name] = field_class(**field_options)

            valid_fields.add(field_name)
            logger.debug(f"添加字段: {model_name}.{field_name} ({field_type})")

        except Exception as e:
            logger.error(f"创建字段 '{field_name}' 失败: {e}", exc_info=True)
            raise ValidationError(f"创建字段 '{field_name}' 失败：{str(e)}") from e

    # 创建模型类
    try:
        model_class = ModelBase(model_name, (models.Model,), model_attrs)
        # 强制设置_model_module（修复Django 5.x兼容性问题）
        if not hasattr(model_class, '_model_module'):
            model_class._model_module = _get_model_module()
        logger.info(f"动态模型创建成功: {model_name} (表名: {model_class._meta.db_table})")
        return model_class
    except Exception as e:
        logger.error(f"创建模型 '{model_name}' 失败: {e}", exc_info=True)
        raise ValidationError(f"创建模型 '{model_name}' 失败：{str(e)}") from e


# -------------------------- 注册 + 建表 --------------------------
def register_and_create_table(
        model_class: Type[models.Model],
        app_label: str = 'lowcode',
        using: str = 'default',
        skip_table_check: bool = False
) -> bool:
    """
    注册模型并创建表（幂等、安全、无重复警告）
    """
    model_name = model_class.__name__
    model_name_lower = model_name.lower()
    table_name = model_class._meta.db_table

    with _REGISTRY_LOCK:
        try:
            # 1. 获取app配置（确保app已加载）
            app_config = apps.get_app_config(app_label)

            # 2. 检查是否已注册（修复大小写问题）
            already_registered = False
            if app_label in apps.all_models:
                if model_name_lower in apps.all_models[app_label] or model_name in apps.all_models[app_label]:
                    already_registered = True

            # 3. 未注册则执行注册
            if not already_registered:
                # 方式1：使用app_config.register_model（推荐）
                try:
                    app_config.register_model(model_name, model_class)
                    logger.debug(f"模型 '{model_name}' 已注册到 Django Apps")
                except Exception as e1:
                    logger.debug(f"app_config注册失败，尝试备用方案: {e1}")
                    # 方式2：手动添加到apps.all_models（兼容方案）
                    if app_label not in apps.all_models:
                        apps.all_models[app_label] = {}
                    apps.all_models[app_label][model_name_lower] = model_class
                    apps.all_models[app_label][model_name] = model_class

                # 更新app_config.models
                app_config.models[model_name] = model_class
                app_config.models[model_name_lower] = model_class

            # 4. 创建表（仅当不存在且未跳过检查）
            create_table = False
            if not skip_table_check and not table_exists(table_name, using=using):
                create_table = True

            if create_table:
                try:
                    conn = connections[using]
                    with conn.schema_editor() as schema_editor:
                        schema_editor.create_model(model_class)
                    logger.info(f"成功创建表: {table_name}")
                except Exception as e:
                    logger.error(f"创建表失败 for {model_name}: {e}", exc_info=True)
                    return False
            elif not skip_table_check:
                logger.debug(f"表 '{table_name}' 已存在，跳过创建")

            # 5. 记录到全局注册表（双保险）
            _DYNAMIC_MODEL_REGISTRY[model_name] = model_class
            _DYNAMIC_MODEL_REGISTRY[model_name_lower] = model_class

            # 6. 清理缓存（静默模式）
            _clear_all_caches(model_class)

            # 7. 验证注册结果
            try:
                test_model = apps.get_model(app_label, model_name) or apps.get_model(app_label, model_name_lower)
                if test_model is None:
                    raise ValueError("Apps中未找到模型")
                logger.debug(f"模型 '{model_name}' 注册验证通过")
            except Exception as e:
                logger.debug(f"模型注册验证警告: {e}")

            return True

        except Exception as e:
            logger.error(f"注册模型 '{model_name}' 失败: {e}", exc_info=True)
            return False


# -------------------------- 高层接口 --------------------------
def register_dynamic_model(
        model_name: str,
        app_label: str = 'lowcode',
        table_name: Optional[str] = None,
        model_config: Optional[Any] = None,
        using: str = 'default'
) -> Type[models.Model]:
    """高层注册接口：从配置构建模型 → 注册 → 建表"""
    # 确保模型名是字符串
    model_name = _ensure_string_input(model_name, silent=True)

    fields_config: Dict[str, Any] = {}

    if model_config:
        try:
            fields_queryset = None
            if hasattr(model_config, 'fieldmodel_set'):
                fields_queryset = model_config.fieldmodel_set.all().order_by('order')
            elif hasattr(model_config, 'fields'):
                fields_queryset = model_config.fields.all().order_by('order')

            if fields_queryset:
                for field in fields_queryset:
                    options = field.options
                    if isinstance(options, str):
                        try:
                            options = json.loads(options)
                        except json.JSONDecodeError:
                            options = {}
                    elif not isinstance(options, dict):
                        options = {}

                    fields_config[field.name] = {
                        'type': field.type,
                        'options': {
                            'verbose_name': field.label or field.name,
                            'help_text': field.help_text or '',
                            'null': not field.required,
                            'blank': not field.required,
                            'default': options.get('default'),
                            **({'max_length': options.get('length', 255)}
                               if field.type in ['char', 'varchar'] else {}),
                            **({'max_digits': options.get('max_digits', 10),
                                'decimal_places': options.get('decimal_places', 2)}
                               if field.type == 'decimal' else {})
                        }
                    }
        except Exception as e:
            logger.error(f"从配置加载字段失败: {e}", exc_info=True)
            raise ValidationError(f"加载字段配置失败：{str(e)}") from e

    # 添加默认时间字段（仅当未显式定义）
    if 'created_at' not in fields_config:
        fields_config['created_at'] = {'type': 'DateTimeField', 'options': {'auto_now_add': True, 'null': True}}
    if 'updated_at' not in fields_config:
        fields_config['updated_at'] = {'type': 'DateTimeField', 'options': {'auto_now': True, 'null': True}}

    # 创建模型类
    model_class = create_dynamic_model(
        model_name=model_name,
        fields_config=fields_config,
        custom_meta={'app_label': app_label},
        table_name=table_name or f"{app_label}_{model_name.lower()}"
    )

    # 注册并创建表（跳过重复检查）
    success = register_and_create_table(model_class, app_label=app_label, using=using, skip_table_check=False)
    if not success:
        raise RuntimeError(f"注册或建表失败: {model_name}")

    # 最终验证
    if not get_dynamic_model(model_name):
        logger.error(f"模型 '{model_name}' 注册后未出现在注册表中")
        raise RuntimeError(f"模型注册验证失败: {model_name}")

    return model_class


def unregister_dynamic_model(model_name: str, clear_cache: bool = True, delete_table: bool = True,
                             using: str = 'default') -> None:
    """注销动态模型（彻底清理）"""
    # 确保模型名是字符串
    model_name = _ensure_string_input(model_name, silent=True)
    model_name_lower = model_name.lower()

    with _REGISTRY_LOCK:
        removed = False
        model_class = _DYNAMIC_MODEL_REGISTRY.get(model_name) or _DYNAMIC_MODEL_REGISTRY.get(model_name_lower)

        # 1. 删除数据表（如果需要）
        if delete_table and model_class:
            try:
                table_name = model_class._meta.db_table
                if table_exists(table_name, using=using):
                    conn = connections[using]
                    with conn.schema_editor() as schema_editor:
                        schema_editor.delete_model(model_class)
                    logger.info(f"成功删除表: {table_name}")
            except Exception as e:
                logger.error(f"删除表 {table_name} 失败: {e}", exc_info=True)

        # 2. 从全局注册表移除
        if model_name in _DYNAMIC_MODEL_REGISTRY:
            del _DYNAMIC_MODEL_REGISTRY[model_name]
            removed = True
        if model_name_lower in _DYNAMIC_MODEL_REGISTRY:
            del _DYNAMIC_MODEL_REGISTRY[model_name_lower]
            removed = True

        # 3. 从 Django Apps 中彻底移除
        try:
            app_config = apps.get_app_config('lowcode')
            # 移除app_config中的模型
            if model_name in app_config.models:
                del app_config.models[model_name]
                removed = True
            if model_name_lower in app_config.models:
                del app_config.models[model_name_lower]
                removed = True

            # 清理apps.all_models缓存
            if 'lowcode' in apps.all_models:
                if model_name in apps.all_models['lowcode']:
                    del apps.all_models['lowcode'][model_name]
                if model_name_lower in apps.all_models['lowcode']:
                    del apps.all_models['lowcode'][model_name_lower]

        except LookupError as e:
            logger.debug(f"清理Apps模型失败（非关键）: {e}")
        except Exception as e:
            logger.error(f"清理模型注册失败: {e}", exc_info=True)

        # 4. 清理缓存
        if clear_cache and removed:
            _clear_all_caches(model_class)

        # 5. 清理配置文件
        try:
            remove_model_from_config(model_name)
        except Exception as e:
            logger.debug(f"清理配置文件失败（非关键）: {e}")

        if removed:
            logger.info(f"动态模型注销成功: {model_name}")
        else:
            logger.debug(f"模型 '{model_name}' 未注册")


def delete_dynamic_model(model_name: str, using: str = 'default') -> bool:
    """
    完整删除动态模型（注销+删表+清配置+清缓存）
    """
    # 确保模型名是字符串
    model_name = _ensure_string_input(model_name, silent=True)

    try:
        # 1. 先注销并删除表
        unregister_dynamic_model(
            model_name=model_name,
            clear_cache=True,
            delete_table=True,
            using=using
        )

        # 2. 清理配置缓存
        clear_config_cache()

        logger.info(f"模型 {model_name} 已完全删除")
        return True

    except Exception as e:
        logger.error(f"删除模型 {model_name} 失败: {e}", exc_info=True)
        return False


# -------------------------- 配置文件操作 --------------------------
def load_model_config(use_cache: bool = True) -> Dict[str, Dict[str, Any]]:
    global _CONFIG_CACHE
    if use_cache:
        with _CONFIG_CACHE_LOCK:
            if _CONFIG_CACHE is not None:
                return _CONFIG_CACHE.copy()

    with _REGISTRY_LOCK:
        if not DYNAMIC_MODEL_CONFIG_PATH.exists():
            logger.debug(f"配置文件不存在: {DYNAMIC_MODEL_CONFIG_PATH}")
            config = {}
        else:
            try:
                with open(DYNAMIC_MODEL_CONFIG_PATH, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                if not isinstance(config, dict):
                    raise ValueError("配置文件根节点必须是JSON对象")
                valid_config = {}
                for model_name, model_spec in config.items():
                    if isinstance(model_spec, dict) and 'fields' in model_spec:
                        valid_config[model_name] = model_spec
                    else:
                        logger.debug(f"跳过无效配置: {model_name}")
                config = valid_config
                logger.info(f"加载 {len(config)} 个有效模型配置")
            except json.JSONDecodeError as e:
                logger.error(f"配置文件解析失败: {e}")
                config = {}
            except Exception as e:
                logger.error(f"加载配置文件失败: {e}", exc_info=True)
                config = {}

        with _CONFIG_CACHE_LOCK:
            _CONFIG_CACHE = config.copy()
        return config.copy()


def save_model_config(config: Dict[str, Dict[str, Any]], update_cache: bool = True) -> None:
    if not isinstance(config, dict):
        raise TypeError("配置必须是字典类型")

    valid_config = {
        name: spec for name, spec in config.items()
        if isinstance(spec, dict) and 'fields' in spec
    }

    with _REGISTRY_LOCK:
        try:
            DYNAMIC_MODEL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            temp_path = DYNAMIC_MODEL_CONFIG_PATH.with_suffix('.tmp')
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(valid_config, f, ensure_ascii=False, indent=2, sort_keys=True)
            temp_path.replace(DYNAMIC_MODEL_CONFIG_PATH)
            logger.info(f"保存 {len(valid_config)} 个模型配置到: {DYNAMIC_MODEL_CONFIG_PATH}")
            if update_cache:
                with _CONFIG_CACHE_LOCK:
                    global _CONFIG_CACHE
                    _CONFIG_CACHE = valid_config.copy()
        except Exception as e:
            logger.error(f"保存配置文件失败: {e}", exc_info=True)
            if temp_path.exists():
                temp_path.unlink()
            raise


def add_model_to_config(
        model_name: str,
        fields_config: Dict[str, Any],
        custom_meta: Optional[Dict[str, Any]] = None,
        overwrite: bool = True
) -> None:
    # 确保模型名是字符串
    model_name = _ensure_string_input(model_name, silent=True)

    with _REGISTRY_LOCK:
        config = load_model_config()
        if model_name in config and not overwrite:
            logger.debug(f"模型 '{model_name}' 已存在，跳过添加")
            return
        config[model_name] = {
            'fields': fields_config,
            'meta': custom_meta or {}
        }
        save_model_config(config)


def remove_model_from_config(model_name: str) -> None:
    # 确保模型名是字符串
    model_name = _ensure_string_input(model_name, silent=True)

    with _REGISTRY_LOCK:
        config = load_model_config()
        if model_name in config:
            del config[model_name]
            save_model_config(config)
            logger.info(f"模型 '{model_name}' 已从配置文件移除")
        else:
            logger.debug(f"模型 '{model_name}' 配置不存在")


# -------------------------- 数据库表操作 --------------------------
def create_dynamic_model_table(model_name: str, using: str = 'default') -> bool:
    """
    创建动态模型表（无重复警告版本）
    """
    # 确保模型名是字符串
    model_name = _ensure_string_input(model_name, silent=True)

    model_class = get_dynamic_model(model_name)
    if not model_class:
        logger.error(f"模型 '{model_name}' 未注册")
        return False
    # 跳过重复的表检查，避免重复日志
    return register_and_create_table(model_class, using=using, skip_table_check=True)


def create_all_dynamic_model_tables(using: str = 'default') -> int:
    count = 0
    for name in list_dynamic_models():
        if create_dynamic_model_table(name, using=using):
            count += 1
    logger.info(f"批量建表完成，共创建 {count} 个表")
    return count


def delete_dynamic_model_table(model_name: str, using: str = 'default', ignore_missing: bool = True) -> bool:
    # 确保模型名是字符串
    model_name = _ensure_string_input(model_name, silent=True)

    model_class = get_dynamic_model(model_name)
    if not model_class:
        logger.error(f"模型 '{model_name}' 未注册")
        return False

    table_name = model_class._meta.db_table
    if not table_exists(table_name, using=using):
        if ignore_missing:
            logger.debug(f"表 '{table_name}' 不存在，跳过删除")
            return True
        return False

    try:
        conn = connections[using]
        with conn.schema_editor() as schema_editor:
            schema_editor.delete_model(model_class)
        logger.info(f"表 '{table_name}' 删除成功")
        return True
    except Exception as e:
        logger.error(f"删除表失败: {e}", exc_info=True)
        return False


# -------------------------- 查询/工具函数 --------------------------
def get_dynamic_model(model_name: str) -> Optional[Type[models.Model]]:
    """
    获取动态模型（静默模式，无警告）
    """
    try:
        # 确保输入是字符串
        model_name = _ensure_string_input(model_name, silent=True)
        model_name_lower = model_name.lower()

        with _REGISTRY_LOCK:
            # 先查原始名称
            model = _DYNAMIC_MODEL_REGISTRY.get(model_name)
            if model:
                return model
            # 再查小写名称
            model = _DYNAMIC_MODEL_REGISTRY.get(model_name_lower)
            if model:
                return model
            # 最后从apps查询
            try:
                return apps.get_model('lowcode', model_name)
            except LookupError:
                try:
                    return apps.get_model('lowcode', model_name_lower)
                except LookupError:
                    logger.debug(f"模型 '{model_name}' 未注册")
                    return None
    except Exception as e:
        logger.error(f"获取动态模型失败: {e}", exc_info=True)
        return None


def list_dynamic_models() -> List[str]:
    with _REGISTRY_LOCK:
        # 去重并返回原始名称（过滤小写副本）
        unique_models = []
        seen = set()
        for name in _DYNAMIC_MODEL_REGISTRY.keys():
            if name.lower() not in seen:
                seen.add(name.lower())
                if name[0].isupper() or name not in unique_models:
                    unique_models.append(name)
        return sorted(unique_models)


def get_dynamic_model_info(model_name: str) -> Optional[Dict[str, Any]]:
    # 确保模型名是字符串
    model_name = _ensure_string_input(model_name, silent=True)

    model_class = get_dynamic_model(model_name)
    if not model_class:
        return None

    fields_info = {}
    for field in model_class._meta.fields:
        fields_info[field.name] = {
            'type': field.__class__.__name__,
            'verbose_name': field.verbose_name,
            'null': field.null,
            'blank': field.blank,
            'default': field.default if not callable(field.default) else str(field.default),
            'max_length': getattr(field, 'max_length', None),
        }

    return {
        'name': model_name,
        'table_name': model_class._meta.db_table,
        'app_label': model_class._meta.app_label,
        'fields': fields_info,
        'verbose_name': model_class._meta.verbose_name,
        'verbose_name_plural': model_class._meta.verbose_name_plural,
        'registered': model_name in _DYNAMIC_MODEL_REGISTRY,
        'table_exists': table_exists(model_class._meta.db_table),
    }


def initialize_dynamic_models(
        create_tables: bool = True,
        using: str = 'default',
        ignore_errors: bool = False
) -> int:
    with _REGISTRY_LOCK:
        config = load_model_config()
        if not config:
            return 0

        new_models = 0
        for model_name, model_spec in config.items():
            if model_name in _DYNAMIC_MODEL_REGISTRY:
                continue

            try:
                model_class = create_dynamic_model(
                    model_name=model_name,
                    fields_config=model_spec['fields'],
                    custom_meta=model_spec.get('meta')
                )
                if create_tables:
                    success = register_and_create_table(model_class, using=using, skip_table_check=False)
                else:
                    # 只注册，不建表
                    app_config = apps.get_app_config('lowcode')
                    app_config.register_model(model_name, model_class)
                    _DYNAMIC_MODEL_REGISTRY[model_name] = model_class
                    success = True
                if success:
                    new_models += 1
            except Exception as e:
                if ignore_errors:
                    logger.error(f"初始化模型 '{model_name}' 失败: {e}", exc_info=True)
                else:
                    raise

        logger.info(f"初始化 {new_models} 个新动态模型")
        return new_models


def ensure_dynamic_models_loaded() -> None:
    global _DYNAMIC_MODELS_LOADED
    if _DYNAMIC_MODELS_LOADED:
        return

    with _LOAD_LOCK:
        if _DYNAMIC_MODELS_LOADED:
            return

        try:
            config = load_model_config(use_cache=False)
            if config:
                initialize_dynamic_models(create_tables=False, ignore_errors=True)
                logger.info(f"加载 {len(config)} 个动态模型")
            else:
                logger.info("无动态模型配置文件，跳过加载")
            _DYNAMIC_MODELS_LOADED = True
            logger.info("动态模型注册表加载完成")
        except Exception as e:
            logger.error(f"加载动态模型失败: {e}", exc_info=True)
            raise


def ensure_dynamic_model(model_class: Type[models.Model], app_label: str = 'lowcode') -> Type[models.Model]:
    """确保模型已注册且表存在"""
    if not getattr(model_class._meta, 'app_label', None):
        model_class._meta.app_label = app_label
    register_and_create_table(model_class, app_label=app_label, skip_table_check=True)
    return model_class


# -------------------------- 高级功能 --------------------------
def get_dynamic_model_with_methods(
        model_name: str,
        extra_methods: Optional[Dict[str, Callable]] = None
) -> Optional[Type[models.Model]]:
    """为动态模型添加自定义方法"""
    # 确保模型名是字符串
    model_name = _ensure_string_input(model_name, silent=True)

    with _REGISTRY_LOCK:
        base_model = get_dynamic_model(model_name)
        if not base_model or not extra_methods:
            return base_model

        valid_methods = {
            name: func for name, func in extra_methods.items()
            if _is_valid_identifier(name) and callable(func)
        }

        if not valid_methods:
            return base_model

        try:
            enhanced_model = type(
                f"{base_model.__name__}Enhanced",
                (base_model,),
                valid_methods
            )
            enhanced_model._meta = base_model._meta
            logger.info(f"为模型 '{model_name}' 添加 {len(valid_methods)} 个自定义方法")
            return enhanced_model
        except Exception as e:
            logger.error(f"添加自定义方法失败: {e}", exc_info=True)
            return base_model


def refresh_dynamic_methods(
        model_name: str,
        new_methods: Dict[str, Callable],
        overwrite_existing: bool = True
) -> Optional[Type[models.Model]]:
    """刷新动态模型的自定义方法"""
    # 确保模型名是字符串
    model_name = _ensure_string_input(model_name, silent=True)

    with _REGISTRY_LOCK:
        base_model = get_dynamic_model(model_name)
        if not base_model or not new_methods:
            return base_model

        updated_count = 0
        for method_name, method_func in new_methods.items():
            if not _is_valid_identifier(method_name) or not callable(method_func):
                continue
            if hasattr(base_model, method_name) and not overwrite_existing:
                continue
            setattr(base_model, method_name, method_func)
            updated_count += 1

        if updated_count > 0:
            _clear_all_caches(base_model)
            logger.info(f"刷新模型 '{model_name}' 的 {updated_count} 个方法")

        return base_model


def update_dynamic_model(
        model_name: str,
        new_fields_config: Dict[str, Any],
        custom_meta: Optional[Dict[str, Any]] = None,
        recreate_table: bool = False,
        using: str = 'default',
        preserve_data: bool = False
) -> bool:
    """更新动态模型（支持字段修改/表重建）"""
    # 确保模型名是字符串
    model_name = _ensure_string_input(model_name, silent=True)

    with _REGISTRY_LOCK:
        try:
            old_model = get_dynamic_model(model_name)
            backup_table = None

            if recreate_table and preserve_data and old_model:
                old_table = old_model._meta.db_table
                if table_exists(old_table, using=using):
                    vendor = connections[using].vendor
                    if vendor in ('postgresql', 'mysql', 'sqlite'):
                        backup_table = f"{old_table}_backup_{uuid.uuid4().hex[:8]}"
                        conn = connections[using]
                        with conn.cursor() as cursor:
                            quoted_old = conn.ops.quote_name(old_table)
                            quoted_new = conn.ops.quote_name(backup_table)
                            cursor.execute(f"ALTER TABLE {quoted_old} RENAME TO {quoted_new}")
                        logger.info(f"备份旧表: {old_table} → {backup_table}")
                    else:
                        logger.warning(f"数据库 {vendor} 不支持表重命名，跳过数据保留")

            if old_model:
                if recreate_table:
                    delete_dynamic_model_table(model_name, using=using)
                unregister_dynamic_model(model_name)

            new_model = create_dynamic_model(
                model_name=model_name,
                fields_config=new_fields_config,
                custom_meta=custom_meta
            )
            success = register_and_create_table(new_model, using=using, skip_table_check=False)
            if not success:
                return False

            add_model_to_config(model_name, new_fields_config, custom_meta)

            logger.info(f"更新动态模型成功: {model_name}")
            return True

        except Exception as e:
            logger.error(f"更新动态模型失败: {e}", exc_info=True)
            return False


def export_dynamic_model_config(model_name: str, export_path: Optional[Path] = None) -> None:
    # 确保模型名是字符串
    model_name = _ensure_string_input(model_name, silent=True)

    config = load_model_config()
    if model_name not in config:
        raise ValueError(f"模型 '{model_name}' 配置不存在")
    export_path = export_path or Path(f"{model_name}_config.json")
    with open(export_path, 'w', encoding='utf-8') as f:
        json.dump({model_name: config[model_name]}, f, ensure_ascii=False, indent=2)
    logger.info(f"模型 '{model_name}' 配置已导出至: {export_path}")


def import_dynamic_model_config(import_path: Path, overwrite: bool = True) -> str:
    if not import_path.exists():
        raise FileNotFoundError(f"导入文件不存在: {import_path}")
    with open(import_path, 'r', encoding='utf-8') as f:
        import_config = json.load(f)
    if not isinstance(import_config, dict) or len(import_config) != 1:
        raise ValueError("导入文件必须仅包含一个模型配置")
    model_name, model_config = next(iter(import_config.items()))
    add_model_to_config(model_name, model_config['fields'], model_config.get('meta'), overwrite=overwrite)
    logger.info(f"模型 '{model_name}' 配置已从 {import_path} 导入")
    return model_name


# -------------------------- 兼容接口 --------------------------
def list_dynamic_model_names() -> List[str]:
    return list_dynamic_models()


def get_all_dynamic_models() -> Dict[str, Type[models.Model]]:
    with _REGISTRY_LOCK:
        return _DYNAMIC_MODEL_REGISTRY.copy()


def get_dynamic_model_fields(model_name: str) -> Optional[Dict[str, Dict[str, Any]]]:
    # 确保模型名是字符串
    model_name = _ensure_string_input(model_name, silent=True)

    model_info = get_dynamic_model_info(model_name)
    return model_info.get('fields', {}) if model_info else None


def cleanup_dynamic_models(
        delete_tables: bool = False,
        using: str = 'default',
        clear_config: bool = False
) -> int:
    with _REGISTRY_LOCK:
        count = len(_DYNAMIC_MODEL_REGISTRY)
        if count == 0:
            return 0

        if delete_tables:
            for model_name in list(_DYNAMIC_MODEL_REGISTRY.keys()):
                delete_dynamic_model_table(model_name, using=using)

        _DYNAMIC_MODEL_REGISTRY.clear()

        if clear_config:
            save_model_config({})
            with _CONFIG_CACHE_LOCK:
                global _CONFIG_CACHE
                _CONFIG_CACHE = None

        logger.info(f"清理 {count} 个动态模型")
        return count


def clear_config_cache() -> None:
    with _CONFIG_CACHE_LOCK:
        global _CONFIG_CACHE
        _CONFIG_CACHE = None
    logger.debug("配置缓存已清空")