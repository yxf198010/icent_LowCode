"""字段解析工具：提取模型业务字段信息"""
import logging
from django.db.models import ForeignKey

logger = logging.getLogger(__name__)


def get_model_business_fields(model):
    """
    获取模型的业务字段（排除系统字段）
    :param model: Django模型类（静态模型或动态模型）
    :return: 业务字段列表，每个字段包含名称、类型、 verbose_name、参数等信息
    """
    if not model or not hasattr(model, '_meta'):
        logger.warning("无效的模型对象，无法获取业务字段")
        return []

    # 系统保留字段：排除不需要展示的基础字段（与models.py中系统字段一致）
    system_fields = ['id', 'create_time', 'update_time']

    business_fields = []
    for field in model._meta.fields:
        # 跳过系统字段
        if field.name in system_fields:
            continue

        # 解析字段类型（处理关联字段特殊情况）
        field_type = field.__class__.__name__
        if field_type == 'ForeignKey':
            # 关联字段：补充关联模型信息
            related_model = field.related_model
            field_info = {
                'name': field.name,
                'type': f'ForeignKey→{related_model.__name__}',
                'verbose_name': getattr(field, 'verbose_name', field.name),
                'null': field.null,
                'blank': field.blank,
                'related_model': related_model.__name__,
                'related_model_label': related_model._meta.verbose_name,
            }
        else:
            # 普通字段：提取核心属性（兼容动态模型字段配置）
            field_info = {
                'name': field.name,
                'type': field_type,
                'verbose_name': getattr(field, 'verbose_name', field.name.replace('_', ' ').title()),
                'null': field.null,
                'blank': field.blank,
                # 处理默认值：避免None/空字符串显示异常
                'default': field.default
                if field.default is not None and field.default != ''
                else '无',
                'help_text': getattr(field, 'help_text', ''),
            }

            # 补充特定字段类型的关键参数（与get_dynamic_model支持的字段类型对应）
            if field_type == 'CharField':
                field_info['max_length'] = getattr(field, 'max_length', 50)
            elif field_type == 'DecimalField':
                field_info['max_digits'] = getattr(field, 'max_digits', 10)
                field_info['decimal_places'] = getattr(field, 'decimal_places', 2)
            elif field_type == 'IntegerField':
                field_info['min_value'] = getattr(field, 'min_value', None)
                field_info['max_value'] = getattr(field, 'max_value', None)
            elif field_type == 'BooleanField':
                field_info['default'] = '是' if field.default else '否'  # 更直观的布尔值显示
            elif field_type in ['DateField', 'DateTimeField']:
                field_info['format'] = 'YYYY-MM-DD' if field_type == 'DateField' else 'YYYY-MM-DD HH:MM:SS'

        business_fields.append(field_info)

    # 按字段在模型中定义的顺序排序（保持一致性）
    return business_fields