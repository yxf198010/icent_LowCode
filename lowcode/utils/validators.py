# validators.py
import json
from django.core.exceptions import ValidationError
from .constants import (
    PYTHON_KEYWORDS,
    SUPPORTED_FIELD_TYPES,
    SYSTEM_RESERVED_FIELD_NAMES
)
from .naming import (
    is_valid_python_class_name,
    is_valid_db_table_name,
    is_valid_field_name
)
from .json_utils import parse_json_array


def validate_model_name(value: str):
    if not value:
        raise ValidationError('模型名称不能为空')
    if not is_valid_python_class_name(value):
        raise ValidationError('模型名称需首字母大写，仅包含字母、数字、下划线')
    if value in PYTHON_KEYWORDS:
        raise ValidationError(f'模型名称"{value}"是Python关键字，不可使用')


def validate_table_name_format(value: str):
    if value and not is_valid_db_table_name(value):
        raise ValidationError('数据表名仅允许字母、数字、下划线，且以字母或下划线开头')


def validate_field_config_json(value: str):
    try:
        return parse_json_array(value)
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        raise ValidationError(f'字段配置格式错误：{str(e)}')


def validate_each_field(field_list: list):
    seen_names = set()
    for idx, field in enumerate(field_list):
        if not isinstance(field, dict):
            raise ValidationError(f'第{idx + 1}个字段配置不是对象格式')

        name = field.get('name', '').strip()
        if not name:
            raise ValidationError(f'第{idx + 1}个字段缺少"name"属性')
        if not is_valid_field_name(name):
            raise ValidationError(
                f'第{idx + 1}个字段名"{name}"不合法：需小写字母开头，仅含字母、数字、下划线'
            )
        if name in seen_names:
            raise ValidationError(f'第{idx + 1}个字段名"{name}"重复')
        if name in SYSTEM_RESERVED_FIELD_NAMES:
            raise ValidationError(f'第{idx + 1}个字段名"{name}"是系统保留字段')
        seen_names.add(name)

        field_type = field.get('type', '').strip()
        if not field_type:
            raise ValidationError(f'第{idx + 1}个字段（{name}）缺少"type"属性')
        if field_type not in SUPPORTED_FIELD_TYPES:
            raise ValidationError(
                f'第{idx + 1}个字段（{name}）类型"{field_type}"不支持。支持类型：{", ".join(SUPPORTED_FIELD_TYPES)}'
            )

        kwargs = field.get('kwargs', {})
        if not isinstance(kwargs, dict):
            raise ValidationError(f'第{idx + 1}个字段（{name}）的"kwargs"必须是对象')

        # 类型特定校验
        if field_type == 'CharField' and 'max_length' not in kwargs:
            raise ValidationError(f'第{idx + 1}个字段（{name}）必须指定 max_length')
        elif field_type == 'DecimalField':
            if 'max_digits' not in kwargs or 'decimal_places' not in kwargs:
                raise ValidationError(f'第{idx + 1}个字段（{name}）必须指定 max_digits 和 decimal_places')
            if not (isinstance(kwargs['max_digits'], int) and kwargs['max_digits'] > 0):
                raise ValidationError(f'第{idx + 1}个字段（{name}）的 max_digits 必须是正整数')
            if not (isinstance(kwargs['decimal_places'], int) and kwargs['decimal_places'] >= 0):
                raise ValidationError(f'第{idx + 1}个字段（{name}）的 decimal_places 必须是非负整数')
        elif field_type == 'BooleanField' and 'default' not in kwargs:
            raise ValidationError(f'第{idx + 1}个字段（{name}）必须指定 default（true/false）')