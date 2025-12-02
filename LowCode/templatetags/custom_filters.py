# lowcode/templatetags/custom_filters.py
from django import template
from django.utils.html import escape, format_html, mark_safe
from django.core.files.storage import default_storage
from django.db.models import NOT_PROVIDED, Model
from datetime import datetime, date, time
from decimal import Decimal
import logging
import os
from functools import reduce
from django.urls import reverse


logger = logging.getLogger(__name__)

# 注册模板库
register = template.Library()

# 字段类型中文映射（与视图保持一致）
FIELD_TYPE_CN_MAP = {
    'CharField': '字符串',
    'TextField': '文本',
    'BooleanField': '布尔值',
    'DateField': '日期',
    'DateTimeField': '日期时间',
    'TimeField': '时间',
    'IntegerField': '整数',
    'FloatField': '浮点数',
    'DecimalField': '小数',
    'EmailField': '邮箱',
    'URLField': '链接',
    'FileField': '文件',
    'ImageField': '图片',
    'UUIDField': 'UUID',
    'ForeignKey': '外键',
    'ManyToManyField': '多对多',
    'AutoField': '自增ID'
}


@register.filter(name='replace')
def replace_filter(value, args):
    """
    替换字符串中的指定部分。
    """
    if not value or ',' not in args:
        return value
    old, new = args.split(',', 1)
    return str(value).replace(old.strip(), new.strip())


@register.filter(name='widget_type')
def widget_type_filter(widget):
    """获取表单组件类型"""
    return widget.__class__.__name__ if widget else ''


@register.filter(name='get_field_value')
def get_field_value_filter(obj, field_name):
    """适配所有字段类型的值格式化，优化版"""
    if not obj or not isinstance(field_name, str) or not field_name.strip():
        return '<span class="status-badge badge-default">-</span>'

    field_name = field_name.strip()
    try:
        value = reduce(getattr, field_name.split('__'), obj)
    except (AttributeError, TypeError):
        return '<span class="status-badge badge-default">-</span>'

    if value in (None, NOT_PROVIDED, ""):
        return '<span class="status-badge badge-default">-</span>'

    # 布尔字段
    if isinstance(value, bool):
        status_text = "启用" if value else "禁用"
        badge_class = "badge-success" if value else "badge-danger"
        return f'<span class="status-badge {badge_class}">{status_text}</span>'

    # 日期时间相关字段
    datetime_formats = {
        datetime: "%Y-%m-%d %H:%M:%S",
        date: "%Y-%m-%d",
        time: "%H:%M:%S"
    }
    for cls, fmt in datetime_formats.items():
        if isinstance(value, cls):
            formatted_value = value.strftime(fmt)
            return f'<span class="date-value text-muted">{formatted_value}</span>'

    # 尝试获取字段实例（缓存结果提升性能）
    field_instance = None
    try:
        if hasattr(obj, '_meta'):
            field_instance = obj._meta.get_field(field_name.split('__')[0])
    except Exception:
        pass

    # 字符串相关字段
    if isinstance(value, str):
        if field_instance and isinstance(field_instance, models.EmailField):
            return f'<a href="mailto:{escape(value)}" class="text-primary">{escape(value)}</a>'
        elif field_instance and isinstance(field_instance, models.URLField):
            return f'<a href="{escape(value)}" target="_blank" class="text-primary" rel="noopener noreferrer">{escape(value)}</a>'
        elif field_instance and isinstance(field_instance, models.UUIDField):
            short_uuid = f"{str(value)[:8]}..." if len(str(value)) > 12 else str(value)
            return f'<span title="{escape(str(value))}">{short_uuid}</span>'
        else:
            stripped_value = value.strip().replace('\n', '<br>')
            if len(stripped_value) > 50:
                return f'<span class="truncate-text" title="{escape(stripped_value.replace("<br>", " "))}">{escape(stripped_value[:50])}...</span>'
            return stripped_value

    # 数字相关字段
    if isinstance(value, (int, float, Decimal)):
        if isinstance(value, (float, Decimal)):
            formatted_value = f"{value:.2f}"
        else:
            formatted_value = f"{value:,}" if abs(value) >= 1000 else str(value)
        return f'<span class="text-info">{formatted_value}</span>'

    # 外键字段
    if field_instance and isinstance(field_instance, models.ForeignKey):
        related_obj = value
        if related_obj and hasattr(related_obj, 'pk'):
            try:
                url = related_obj.get_absolute_url() if hasattr(related_obj,
                                                                'get_absolute_url') else f'/{related_obj._meta.app_label}/{related_obj._meta.model_name}/detail/{related_obj.pk}/'
                return f'<a href="{url}" class="text-info">{escape(str(related_obj))}</a>'
            except Exception:
                return f'<span class="text-secondary">{escape(str(related_obj))}</span>'
        return '<span class="status-badge badge-default">-</span>'

    # 多对多字段
    if field_instance and isinstance(field_instance, models.ManyToManyField):
        related_objs = value.all() if hasattr(value, 'all') else []
        tags = [f'<span class="badge bg-secondary">{escape(str(rel_obj))}</span>' for rel_obj in related_objs[:5]]
        total = related_objs.count()
        if total > 5:
            tags.append(f'<span class="badge bg-light text-dark">+{total - 5}</span>')
        return " ".join(tags) if tags else '<span class="status-badge badge-default">-</span>'

    # 文件/图片字段
    if field_instance and isinstance(field_instance, (models.FileField, models.ImageField)):
        if getattr(value, 'name', None):
            file_name = os.path.basename(value.name)
            try:
                file_url = default_storage.url(file_name)
                if isinstance(field_instance, models.ImageField):
                    return f'<a href="{file_url}" target="_blank" rel="noopener noreferrer"><img src="{file_url}" alt="{escape(file_name)}" class="img-thumbnail-small"></a>'
                return f'<a href="{file_url}" target="_blank" rel="noopener noreferrer" class="file-link"><i class="bi bi-file-earmark"></i> {escape(file_name)}</a>'
            except Exception as e:
                logger.warning(f"Failed to access file: {e}")
                return f'<span class="text-warning" title="文件访问失败">{escape(file_name)}</span>'
        return '<span class="status-badge badge-default">-</span>'

    return escape(str(value))


@register.filter
def basename(value):
    """从文件路径中提取文件名"""
    return os.path.basename(str(value)) if value else ''


@register.filter
def add_class(field, css_class):
    if not isinstance(field, BoundField):
        return field
    existing = field.field.widget.attrs.get('class', '')
    field.field.widget.attrs['class'] = f"{existing} {css_class}".strip()
    return field.as_widget()


@register.filter
def get_field_display_info(obj, field_name):
    """返回字段的结构化显示信息，供模板安全渲染"""
    # 省略以保持简洁，您可以根据需要添加此函数的具体实现
    pass


@register.filter
def sum(queryset, attr):
    """计算查询集指定属性的总和"""
    return sum(getattr(item, attr, 0) for item in queryset)


@register.filter
def divide(value, divisor):
    """除法运算，避免除以0"""
    return value / divisor if divisor != 0 else 0




@register.filter
def get_field_display_info(obj, field_name):
    """
    返回字段的结构化显示信息，供模板安全渲染
    """
    field = obj._meta.get_field(field_name)
    value = getattr(obj, field_name, None)

    info = {
        'is_boolean': False,
        'is_image': False,
        'is_file': False,
        'is_foreignkey': False,
        'is_many_to_many': False,
        'is_date': False,
        'is_datetime': False,
        'is_long_text': False,
        'is_empty': value is None or (hasattr(value, '__len__') and len(value) == 0),
        'display_value': '-',
        'url': '',
        'filename': '',
        'css_class': '',
    }

    # 空值处理
    if info['is_empty']:
        return info

    # 布尔字段
    if field.get_internal_type() == 'BooleanField':
        info['is_boolean'] = True
        info['display_value'] = '是' if value else '否'
        info['css_class'] = 'tag-active' if value else 'tag-inactive'
        return info

    # 日期/时间字段
    if field.get_internal_type() in ('DateField', 'DateTimeField'):
        if field.get_internal_type() == 'DateField':
            info['is_date'] = True
            info['display_value'] = date_format(value, 'Y-m-d')
        else:
            info['is_datetime'] = True
            info['display_value'] = date_format(value, 'Y-m-d H:i')
        return info

    # 文件/图片字段（假设使用 FileField 或 ImageField）
    if hasattr(field, 'upload_to') and value:
        info['url'] = value.url
        info['filename'] = os.path.basename(value.name)
        if field.get_internal_type() == 'ImageField':
            info['is_image'] = True
        else:
            info['is_file'] = True
        return info

    # 外键
    if field.many_to_one:
        info['is_foreignkey'] = True
        if value:
            # 尝试生成详情页链接
            try:
                url = reverse('lowcode:dynamic_model_detail', kwargs={
                    'model_name': value._meta.model_name,
                    'pk': value.pk
                })
                info['display_value'] = mark_safe(f'<a href="{url}" class="text-info">{str(value)}</a>')
            except:
                info['display_value'] = str(value)
        else:
            info['display_value'] = '-'
        return info

    # 多对多
    if field.many_to_many:
        info['is_many_to_many'] = True
        related_objs = value.all()
        if related_objs.exists():
            badges = []
            for rel_obj in related_objs[:5]:  # 最多显示5个
                try:
                    url = reverse('lowcode:dynamic_model_detail', kwargs={
                        'model_name': rel_obj._meta.model_name,
                        'pk': rel_obj.pk
                    })
                    badge = f'<a href="{url}" class="badge bg-light text-dark text-decoration-none me-1">{rel_obj}</a>'
                except:
                    badge = f'<span class="badge bg-light text-dark me-1">{rel_obj}</span>'
                badges.append(badge)
            if related_objs.count() > 5:
                badges.append(f'<span class="badge bg-secondary">+{related_objs.count()-5}</span>')
            info['display_value'] = mark_safe(''.join(badges))
        else:
            info['display_value'] = '-'
        return info

    # 普通文本/长文本
    display_str = str(value) if value is not None else '-'
    info['display_value'] = display_str
    if len(display_str) > 50:
        info['is_long_text'] = True

    return info

@register.filter
def get_class_name(value):
    """返回对象的类名，用于模板中判断字段类型"""
    return value.__class__.__name__


@register.filter(name='rejectattr')
def rejectattr(items, attr_name):
    """
    过滤掉具有指定属性的项目
    用法: {{ items|rejectattr:"some_attr" }}
    """
    if not items:
        return []
    return [item for item in items if not hasattr(item, attr_name) or not getattr(item, attr_name)]


@register.filter(name='rejectattr_equal')
def rejectattr_equal(items, attr_value):
    """
    过滤掉指定属性等于特定值的项目
    用法: {{ items|rejectattr_equal:"some_attr:value" }}
    """
    if not items or ':' not in attr_value:
        return []

    attr_name, value = attr_value.split(':', 1)
    return [item for item in items if not hasattr(item, attr_name) or getattr(item, attr_name) != value]


@register.filter(name='rejectattr')
def rejectattr(items, attr_name):
    """
    过滤掉具有指定属性的项目
    用法: {{ items|rejectattr:"some_attr" }}
    """
    if not items:
        return []
    return [item for item in items if not hasattr(item, attr_name) or not getattr(item, attr_name)]


@register.filter(name='rejectattr_equal')
def rejectattr_equal(items, attr_value):
    """
    过滤掉指定属性等于特定值的项目
    用法: {{ items|rejectattr_equal:"some_attr:value" }}
    """
    if not items or ':' not in attr_value:
        return []

    attr_name, value = attr_value.split(':', 1)
    return [item for item in items if not hasattr(item, attr_name) or getattr(item, attr_name) != value]


# 添加 list 过滤器
@register.filter(name='list')
def to_list(value):
    """
    将可迭代对象转换为列表（处理 QuerySet、生成器等）
    用法: {{ queryset|list }}
    """
    if not value:
        return []
    # 尝试将各种可迭代对象转换为列表
    try:
        return list(value)
    except:
        return [value] if value is not None else []


@register.filter(name='list_join')
def list_join(items, separator=','):
    """
    将列表项连接为字符串
    用法: {{ items|list_join:", " }}
    """
    if not items:
        return ""
    # 确保是列表
    items = to_list(items)
    # 转换所有项为字符串
    return separator.join(str(item) for item in items if item is not None)
