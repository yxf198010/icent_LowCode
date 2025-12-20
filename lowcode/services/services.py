# lowcode/services/services.py
import logging
from typing import Optional, Dict, Any, Callable
from types import ModuleType

from django.apps import apps
from django.db import connection, transaction
from django.db import models

from lowcode.models import LowCodeModelConfig
from lowcode.utils.naming import is_valid_python_class_name, is_valid_field_name
from lowcode.utils.json_utils import parse_json_array

logger = logging.getLogger(__name__)


# 字段类型映射：Django 字段类路径
FIELD_TYPE_MAP: Dict[str, str] = {
    'CharField': 'django.db.models.CharField',
    'TextField': 'django.db.models.TextField',
    'IntegerField': 'django.db.models.IntegerField',
    'BooleanField': 'django.db.models.BooleanField',
    'EmailField': 'django.db.models.EmailField',
    'DecimalField': 'django.db.models.DecimalField',
    'DateTimeField': 'django.db.models.DateTimeField',
    'DateField': 'django.db.models.DateField',
    'ForeignKey': 'django.db.models.ForeignKey',
}

# 字段类型到 SQL 的映射（用于 ensure_db_table_exists）
def _char_sql(kwargs: Dict[str, Any]) -> str:
    return f"VARCHAR({kwargs.get('max_length', 255)})"

def _decimal_sql(kwargs: Dict[str, Any]) -> str:
    max_digits = kwargs.get('max_digits', 10)
    decimal_places = kwargs.get('decimal_places', 2)
    return f"DECIMAL({max_digits}, {decimal_places})"

TYPE_SQL_MAP: Dict[str, Callable[[Dict[str, Any]], str]] = {
    'CharField': _char_sql,
    'TextField': lambda _: "TEXT",
    'IntegerField': lambda _: "INTEGER",
    'BooleanField': lambda _: "BOOLEAN",
    'EmailField': lambda _: "VARCHAR(254)",
    'DecimalField': _decimal_sql,
    'DateTimeField': lambda _: "TIMESTAMP",
    'DateField': lambda _: "DATE",
}


def create_or_update_lowcode_model(
    name: str,
    fields_config: str,
    table_name: Optional[str] = None,
    instance_id: Optional[int] = None
) -> LowCodeModelConfig:
    """
    创建或更新一个低代码模型元数据记录，并尝试注册为运行时模型（仅限当前进程）

    Args:
        name: 模型类名（如 'Product'）
        fields_config: JSON 字符串，字段配置数组
        table_name: 数据库表名（若为空则自动生成）
        instance_id: 若提供，则为更新操作

    Returns:
        LowCodeModelConfig 实例
    """
    from ..forms import LowCodeModelConfigForm  # 避免循环导入

    form_data = {
        'name': name.strip(),
        'table_name': table_name.strip() if table_name else '',
        'fields': fields_config.strip(),
    }

    instance = None
    if instance_id:
        try:
            instance = LowCodeModelConfig.objects.get(id=instance_id)
        except LowCodeModelConfig.DoesNotExist:
            raise ValueError(f"ID 为 {instance_id} 的模型不存在")

    form = LowCodeModelConfigForm(form_data, instance=instance)

    if not form.is_valid():
        error_messages = [
            f"{field}: {err}"
            for field, errors in form.errors.items()
            for err in errors
        ]
        raise ValueError("模型配置无效:\n" + "\n".join(error_messages))

    with transaction.atomic():
        model_instance = form.save()
        logger.info(f"{'更新' if instance_id else '创建'}低代码模型: {model_instance.name} (表: {model_instance.table_name})")

        # 尝试注册动态模型（不影响元数据保存）
        try:
            register_dynamic_model(model_instance)
        except Exception as e:
            logger.warning(f"动态模型注册失败（不影响元数据保存）: {e}")

        # 可选：自动创建数据库表（可根据需求开关）
        try:
            ensure_db_table_exists(model_instance)
        except Exception as e:
            logger.error(f"自动创建数据库表失败: {e}")
            raise  # 或根据策略决定是否抛出

    return model_instance


def register_dynamic_model(model_meta: LowCodeModelConfig):
    """
    （实验性）将 LowCodeModelConfig 实例动态注册为可用的 Django 模型类
    注意：此模型不会出现在 migrations 中，仅在当前进程内存中有效
    """
    app_label = model_meta._meta.app_label
    model_name = model_meta.name

    if not is_valid_python_class_name(model_name):
        raise ValueError(f"无效模型类名: {model_name}")

    raw_fields = parse_json_array(model_meta.fields)

    # 检查是否已注册（避免重复）
    try:
        existing = apps.get_model(app_label, model_name)
        if existing:
            logger.debug(f"模型 {model_name} 已存在，跳过注册")
            return existing
    except LookupError:
        pass  # 模型不存在，继续注册

    model_fields = {
        '__module__': f'{app_label}.models',
        'Meta': type('Meta', (), {
            'db_table': model_meta.table_name,
            'app_label': app_label,
            'managed': False,
        }),
    }

    for field_def in raw_fields:
        name = field_def['name']
        field_type_str = field_def['type']
        kwargs = field_def.get('kwargs', {})

        if not is_valid_field_name(name):
            raise ValueError(f"无效字段名: {name}")

        if field_type_str not in FIELD_TYPE_MAP:
            raise ValueError(f"不支持的字段类型: {field_type_str}")

        # 动态导入字段类
        module_path, class_name = FIELD_TYPE_MAP[field_type_str].rsplit('.', 1)
        module: ModuleType = __import__(module_path, fromlist=[class_name])
        field_class = getattr(module, class_name)

        if field_type_str == 'ForeignKey':
            to_model = kwargs.pop('to', None)
            if not to_model:
                raise ValueError(f"ForeignKey 字段 '{name}' 必须指定 'to'")
            model_fields[name] = field_class(to_model, **kwargs)
        else:
            model_fields[name] = field_class(**kwargs)

    # 继承自 models.Model，而非 LowCodeModelConfig
    dynamic_model = type(model_name, (models.Model,), model_fields)

    # 注册到 apps
    try:
        apps.register_model(app_label, dynamic_model)
        # 手动创建数据库表
        with connection.schema_editor() as schema_editor:
            schema_editor.create_model(dynamic_model)
        logger.debug(f"动态模型 {model_name} 已注册到 app '{app_label}'")
    except RuntimeError as e:
        if "already imported" in str(e):
            logger.debug(f"模型 {model_name} 已注册（热重载场景），跳过")
        else:
            raise

    return dynamic_model


def ensure_db_table_exists(model_meta: LowCodeModelConfig):
    """
    确保数据库表存在，若不存在则创建（仅支持基础字段类型）
    ⚠️ 警告：直接操作 schema，建议仅用于原型或受控环境
    """
    table_name = model_meta.table_name
    inspector = connection.introspection
    existing_tables = inspector.table_names()

    if table_name in existing_tables:
        logger.debug(f"数据库表 {table_name} 已存在")
        return

    raw_fields = parse_json_array(model_meta.fields)
    fields_sql = ["id SERIAL PRIMARY KEY"]

    for field_def in raw_fields:
        name = field_def['name']
        field_type = field_def['type']
        kwargs = field_def.get('kwargs', {})

        # 跳过 ForeignKey 的反向字段（实际列由 to 表决定，此处不创建）
        if field_type == 'ForeignKey':
            # 正常情况下，ForeignKey 会创建 <name>_id 列
            col_name = f"{name}_id"
            fields_sql.append(f"{col_name} INTEGER")  # 简化：假设所有 FK 是整数
            continue

        if field_type not in TYPE_SQL_MAP:
            logger.warning(f"跳过不支持用于建表的字段类型: {field_type} ({name})")
            continue

        try:
            col_def = f"{name} {TYPE_SQL_MAP[field_type](kwargs)}"
            fields_sql.append(col_def)
        except Exception as e:
            logger.error(f"生成字段 {name} 的 SQL 失败: {e}")
            raise ValueError(f"字段 {name} 的配置无法转换为 SQL: {e}")

    sql = f"CREATE TABLE {connection.ops.quote_name(table_name)} ({', '.join(fields_sql)});"
    with connection.cursor() as cursor:
        cursor.execute(sql)
    logger.info(f"数据库表 {table_name} 已创建")