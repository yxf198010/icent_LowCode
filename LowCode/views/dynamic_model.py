# lowcode/views/dynamic_model.py
# 动态模型相关代码（CRUD 视图、模型创建 / 列表 / 升级、动态方法调用、VUE 交互 API）
import logging
import re
import json
from typing import Any, Dict, List, Type, Optional
from datetime import datetime, date
from uuid import uuid4

from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.cache import cache
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, HttpRequest, JsonResponse, HttpResponse
from django.db import connection, models, transaction
from django.db.models import Q
from django.shortcuts import render, get_object_or_404, redirect, reverse
from django.utils import timezone
from django.utils.text import capfirst
from django.views.generic import ListView, CreateView, DetailView, UpdateView, DeleteView
from django import forms
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.core.paginator import Paginator, EmptyPage, PageNotAnInteger
from django.contrib.auth.decorators import login_required

# 本地导入
from lowcode.models.models import LowCodeModelConfig, FieldModel, Role
from lowcode.dynamic_model_registry import (
    get_dynamic_model,
    list_dynamic_models,
    ensure_dynamic_models_loaded,
    create_dynamic_model_table,
    get_dynamic_model_with_methods,
    refresh_dynamic_methods,
    list_dynamic_model_names,
    unregister_dynamic_model  # 新增：导入注销动态模型函数
)
from lowcode.forms import LowCodeModelConfigForm

# DRF 导入（仅用于 API 视图）
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework import status

from lowcode.dynamic_model_registry import delete_dynamic_model
from django.views.decorators.http import require_POST
from django.db import ProgrammingError

# 配置
CACHE_TIMEOUT = 60
logger = logging.getLogger(__name__)

ModelConfigType = Dict[str, Any]
OverviewDataType = Dict[str, Any]


# ========== 通用缓存清理工具函数 ==========
def safe_clear_model_cache(model_name: str = None):
    """
    安全清理模型相关缓存（兼容所有缓存后端）
    :param model_name: 模型名称（None 表示清理所有模型缓存）
    """
    try:
        # 1. 定义需要清理的缓存键列表
        cache_keys = []
        if model_name:
            # 清理指定模型的缓存
            cache_keys.extend([
                f'lowcode_field_count_{model_name}',
                f'lowcode_data_count_{model_name}',
                f'dynamic_model_{model_name}',
            ])
        else:
            # 清理所有模型相关缓存（谨慎使用）
            cache_keys.extend([
                'dynamic_model_list',
                'dynamic_model_tables',
            ])

        # 2. 批量删除明确的缓存键（所有后端都支持）
        if cache_keys:
            cache.delete_many(cache_keys)

        # 3. 尝试模式匹配删除（仅对支持的后端生效）
        if model_name:
            patterns = [
                f'lowcode_field_count_{model_name}*',
                f'lowcode_data_count_{model_name}*',
                f'dynamic_model_{model_name}*',
            ]
        else:
            patterns = [
                'lowcode_field_count_*',
                'lowcode_data_count_*',
                'dynamic_model_*',
            ]

        # 检查缓存后端是否支持 delete_pattern
        if hasattr(cache, 'delete_pattern'):
            for pattern in patterns:
                cache.delete_pattern(pattern)
        else:
            # 对 LocMemCache 尝试安全的 keys 遍历（仅当 keys 方法存在时）
            if hasattr(cache, 'keys'):
                try:
                    all_keys = cache.keys('*')
                    match_keys = []
                    for pattern in patterns:
                        # 简单的通配符匹配（仅处理 * 结尾）
                        prefix = pattern.rstrip('*')
                        match_keys.extend([k for k in all_keys if k.startswith(prefix)])
                    if match_keys:
                        cache.delete_many(match_keys)
                except Exception:
                    # LocMemCache 的 keys 方法可能触发异常，直接跳过
                    pass
    except Exception as e:
        # 仅记录警告，不中断主流程
        logger.warning(f"缓存清理警告（非关键）：{str(e)}")


# ========== 工具函数 ==========
def get_model_by_name(model_name: str) -> Type[models.Model]:
    model_class = get_dynamic_model(model_name)
    if model_class is None:
        logger.warning(f"模型 '{model_name}' 不存在或未注册")
        raise Http404(f"模型 '{model_name}' 不存在或未注册")
    return model_class


def get_model_record_count(model_name: str) -> int | str:
    """
    修复：增加数据表存在性校验，避免查询已删除的表
    返回值：记录数（int）| "已删除" | "统计失败"
    """
    key = f'lowcode_data_count_{model_name}'
    cnt = cache.get(key)
    if cnt is None:
        try:
            # 第一步：检查数据表是否存在
            table_name = f"lowcode_{model_name.lower()}"
            with connection.cursor() as cursor:
                cursor.execute("""
                               SELECT EXISTS (SELECT
                                              FROM information_schema.tables
                                              WHERE table_name = %s);
                               """, [table_name])
                table_exists = cursor.fetchone()[0]

            if not table_exists:
                cnt = "已删除"
                cache.set(key, cnt, CACHE_TIMEOUT)
                return cnt

            # 第二步：表存在则统计记录数
            model_class = get_dynamic_model(model_name)
            cnt = model_class.objects.count() if model_class else 0
            cache.set(key, cnt, CACHE_TIMEOUT)
        except Exception as e:
            logger.error(f"统计模型 '{model_name}' 记录数失败: {str(e)}")
            cnt = "统计失败"
            cache.set(key, cnt, CACHE_TIMEOUT)
    return cnt


def build_dynamic_form(model_class: Type[models.Model]) -> Type[forms.ModelForm]:
    meta_attrs = {'model': model_class, 'fields': '__all__'}
    form_class = type(
        f'{model_class.__name__}Form',
        (forms.ModelForm,),
        {'Meta': type('Meta', (), meta_attrs)}
    )
    return configure_form_fields(form_class, model_class)


def configure_form_fields(form_class, model: models.Model):
    model_fields = {f.name: f for f in model._meta.get_fields() if hasattr(f, 'name')}
    form_fields = form_class.base_fields

    def get_base_attrs(label: str, model_field: models.Field) -> Dict[str, Any]:
        return {
            'class': 'form-control',
            'placeholder': f'请输入{label}',
            'required': not (model_field.blank or model_field.null),
        }

    field_type_config = {
        models.BooleanField: {
            'widget': forms.Select(choices=[(True, '是'), (False, '否')], attrs={'class': 'form-select'}),
            'initial': lambda mf: mf.default if mf.default is not models.NOT_PROVIDED else True
        },
        models.DateField: {
            'widget': lambda attrs: forms.DateInput(attrs={**attrs, 'type': 'date'}),
            'initial': lambda mf: date.today() if mf.default is models.NOT_PROVIDED and not (
                    mf.blank or mf.null) else None
        },
        models.DateTimeField: {
            'widget': lambda attrs: forms.DateTimeInput(attrs={**attrs, 'type': 'datetime-local'}),
            'initial': lambda mf: timezone.now().strftime(
                '%Y-%m-%dT%H:%M') if mf.default is models.NOT_PROVIDED and not (mf.blank or mf.null) else None
        },
        models.TimeField: {
            'widget': lambda attrs: forms.TimeInput(attrs={**attrs, 'type': 'time'})
        },
        models.EmailField: {
            'widget': forms.EmailInput
        },
        models.URLField: {
            'widget': lambda attrs: forms.URLInput(attrs={**attrs, 'placeholder': '例如 https://example.com'})
        },
        (models.IntegerField, models.FloatField, models.DecimalField): {
            'widget': lambda attrs: forms.NumberInput(attrs=attrs),
            'attrs_modifier': lambda mf, attrs: {
                **attrs,
                'min': getattr(mf, 'min_value', None),
                'max': getattr(mf, 'max_value', None),
                'step': '0.01' if isinstance(mf, (models.FloatField, models.DecimalField)) else '1'
            }
        },
        models.CharField: {
            'attrs_modifier': lambda mf, attrs: {**attrs, 'maxlength': mf.max_length} if hasattr(mf,
                                                                                                 'max_length') else attrs
        },
        models.TextField: {
            'widget': lambda attrs: forms.Textarea(
                attrs={'class': 'form-control', 'rows': 4, 'required': attrs['required']})
        },
        models.FileField: {
            'widget': lambda attrs: forms.FileInput(attrs={**attrs, 'accept': 'image/*'}) if isinstance(
                model_fields.get(field_name),
                models.ImageField) else forms.FileInput(
                attrs=attrs)
        },
        models.ForeignKey: {
            'widget': forms.Select(attrs={'class': 'form-select'}),
            'queryset': lambda mf: mf.related_model.objects.all().order_by('pk')
        },
        models.ManyToManyField: {
            'widget': forms.SelectMultiple(attrs={'class': 'form-select', 'size': 5}),
            'queryset': lambda mf: mf.related_model.objects.all().order_by('pk')
        },
        models.UUIDField: {
            'initial': lambda mf: str(uuid4()) if mf.default is models.NOT_PROVIDED else None
        }
    }

    for field_name, form_field in form_fields.items():
        model_field = model_fields.get(field_name)
        if not model_field or not hasattr(model_field, 'verbose_name'):
            continue

        label = model_field.verbose_name or capfirst(field_name.replace('_', ' '))
        form_field.label = label
        base_attrs = get_base_attrs(label, model_field)

        for field_types, config in field_type_config.items():
            if isinstance(model_field, field_types):
                if 'widget' in config:
                    if callable(config['widget']):
                        form_field.widget = config['widget'](base_attrs)
                    else:
                        form_field.widget = config['widget'](attrs=base_attrs)

                if 'attrs_modifier' in config and callable(config['attrs_modifier']):
                    form_field.widget.attrs = config['attrs_modifier'](model_field, form_field.widget.attrs)

                if 'initial' in config and callable(config['initial']) and form_field.initial is None:
                    initial_value = config['initial'](model_field)
                    if initial_value is not None:
                        form_field.initial = initial_value

                if 'queryset' in config and callable(config['queryset']):
                    form_field.queryset = config['queryset'](model_field)

                break
        else:
            form_field.widget.attrs.update(base_attrs)

    # 修复：在 enhanced_clean 中使用局部变量 base_attrs 会报错，应重新获取
    original_clean = getattr(form_class, 'clean', lambda self: super(type(self), self).clean())

    def enhanced_clean(self):
        cleaned_data = original_clean(self)
        for field_name, value in cleaned_data.items():
            model_field = model_fields.get(field_name)
            if not model_field:
                continue
            label = self[field_name].label or field_name

            # 获取当前字段的 required 状态
            required = not (model_field.blank or model_field.null)

            # 字符串字段去空格
            if isinstance(model_field, (models.CharField, models.TextField)) and isinstance(value, str):
                cleaned_data[field_name] = value.strip()
                if required and not cleaned_data[field_name]:
                    self.add_error(field_name, f'{label}不能为空')

            # 日期时间验证
            elif isinstance(model_field, models.DateField) and value and value > date.today():
                self.add_error(field_name, f'{label}不能晚于当前日期')
            elif isinstance(model_field, models.DateTimeField) and value and value > timezone.now():
                self.add_error(field_name, f'{label}不能晚于当前时间')

            # 数值范围验证
            elif isinstance(model_field,
                            (models.IntegerField, models.FloatField, models.DecimalField)) and value is not None:
                min_val = getattr(model_field, 'min_value', None)
                max_val = getattr(model_field, 'max_value', None)
                if min_val is not None and value < min_val:
                    self.add_error(field_name, f'{label}不能小于{min_val}')
                if max_val is not None and value > max_val:
                    self.add_error(field_name, f'{label}不能大于{max_val}')

            # 文件大小验证
            elif isinstance(model_field, (models.FileField, models.ImageField)) and value and hasattr(value, 'size'):
                max_size = 10 * 1024 * 1024  # 10MB
                if value.size > max_size:
                    self.add_error(field_name, f'{label}大小不能超过10MB（当前{value.size // 1024 // 1024}MB）')

        return cleaned_data

    form_class.clean = enhanced_clean
    return form_class


def enhance_form_fields(form, model_class: Type[models.Model]) -> List[Dict[str, Any]]:
    fields_info = []
    for field_name, form_field in form.fields.items():
        model_field = model_class._meta.get_field(field_name)
        widget = form_field.widget

        if isinstance(widget, forms.CheckboxInput):
            widget_type = 'checkbox'
        elif isinstance(widget, forms.Textarea):
            widget_type = 'ckeditor'
        elif isinstance(widget, forms.FileInput):
            widget_type = 'image' if isinstance(model_field, models.ImageField) else 'file'
        elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
            widget_type = 'select'
        else:
            widget_type = 'default'

        fields_info.append({
            'name': field_name,
            'label': form_field.label or field_name,
            'bound_field': form[field_name],
            'widget_type': widget_type,
            'help_text': getattr(form_field, 'help_text', ''),
            'required': form_field.required
        })
    return fields_info


def get_all_dynamic_model_configs() -> List[ModelConfigType]:
    """修复：过滤已删除数据表的模型配置，避免前端显示无效模型"""
    ensure_dynamic_models_loaded()
    existing_tables = {t.lower() for t in connection.introspection.table_names()}
    model_names = list_dynamic_model_names()

    configs_queryset = LowCodeModelConfig.objects.filter(name__in=model_names).only(
        'id', 'name', 'table_name', 'create_time', 'update_time'
    )
    config_map = {cfg.name: cfg for cfg in configs_queryset}

    model_configs = []
    for name in model_names:
        config = config_map.get(name)
        if not config:
            continue

        table = (config.table_name if config and config.table_name else name).lower()

        field_count_key = f'lowcode_field_count_{name}'
        field_count = cache.get(field_count_key)
        if field_count is None:
            if config:
                try:
                    field_count = FieldModel.objects.filter(model_config=config).count()
                except Exception:
                    field_count = 0
            else:
                field_count = 0
            cache.set(field_count_key, field_count, CACHE_TIMEOUT)

        # 修复：仅添加数据表存在的模型配置
        model_configs.append({
            'name': name,
            'config': config,
            'has_config': config is not None,
            'exists_in_db': table in existing_tables,
            'record_count': get_model_record_count(name),
            'field_count': field_count,
            'table_name': table,
            'create_time': config.create_time if config else None,
            'update_time': config.update_time if config else None,
            'status_cn': '已配置' if (
                        config and field_count > 0 and table in existing_tables) else '已删除' if config and table not in existing_tables else '未配置',
            'status_type': 'success' if (
                        config and field_count > 0 and table in existing_tables) else 'danger' if config and table not in existing_tables else 'secondary'
        })

    # 过滤掉已删除的模型（可选：保留但标记状态）
    # model_configs = [cfg for cfg in model_configs if cfg['exists_in_db']]

    return sorted(model_configs, key=lambda x: x['create_time'] or datetime.min, reverse=True)


# ========== 表单验证类 ==========
class FieldForm(forms.Form):
    name = forms.CharField(
        max_length=100,
        validators=[
            lambda x: re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', x) or ValidationError('字段名称必须是合法Python标识符')],
        error_messages={'required': '字段名称不能为空'}
    )
    type = forms.ChoiceField(
        choices=[
            ('char', '字符串 (CHAR)'),
            ('varchar', '变长字符串 (VARCHAR)'),
            ('int', '整数 (INT)'),
            ('bigint', '长整数 (BIGINT)'),
            ('decimal', '小数 (DECIMAL)'),
            ('text', '文本 (TEXT)'),
            ('datetime', '日期时间 (DATETIME)'),
            ('boolean', '布尔 (BOOLEAN)'),
        ],
        error_messages={'required': '字段类型不能为空'}
    )
    length = forms.IntegerField(
        required=False,
        min_value=1,
        error_messages={'min_value': '长度必须为正整数'}
    )
    required = forms.BooleanField(required=False)
    default = forms.CharField(required=False)
    comment = forms.CharField(required=False)


class ModelForm(forms.Form):
    name = forms.CharField(
        max_length=50,
        validators=[
            lambda x: re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', x) or ValidationError('模型名称必须是合法Python标识符')],
        error_messages={'required': '模型名称不能为空'}
    )
    table_name = forms.CharField(
        max_length=100,
        validators=[lambda x: re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', x) or ValidationError('数据表名必须是合法标识符')],
        error_messages={'required': '数据表名不能为空'}
    )
    roles = forms.MultipleChoiceField(required=False)


# ========== 核心修复：创建字段时正确处理布尔型默认值 ==========
def create_field_with_default(model_config, field_data, idx):
    """
    创建字段并正确处理默认值（重点修复布尔字段）
    """
    # 解析字段类型
    field_type = field_data['type']

    # 构建字段选项（包含长度、默认值等信息）
    options = {
        'length': field_data.get('length'),
        'default': field_data.get('default'),
        'comment': field_data.get('comment')
    }

    # 特殊处理布尔字段默认值
    if field_type == 'boolean':
        # 转换默认值为布尔类型
        default_val = field_data.get('default')
        if default_val is not None:
            if isinstance(default_val, str):
                # 处理字符串形式的布尔值
                if default_val.lower() in ['true', '1', '是']:
                    options['default'] = True
                elif default_val.lower() in ['false', '0', '否']:
                    options['default'] = False
            else:
                options['default'] = bool(default_val)
        else:
            # 布尔字段默认值为空时，设置默认值为 True
            options['default'] = True

    # ========== 修复步骤3：强制设置help_text为非空字符串 ==========
    help_text = field_data.get('comment', '') or ''

    # 创建字段记录
    field = FieldModel.objects.create(
        model_config=model_config,
        model_name=model_config.name,
        name=field_data['name'],
        label=field_data.get('comment', field_data['name']),  # 用备注作为标签，无则用字段名
        type=field_type,
        required=field_data.get('required', False),
        help_text=help_text,  # 强制非空（空字符串）
        options=json.dumps(options),  # 改为JSON存储
        order=idx  # 保留order字段实现排序
    )

    return field


# ========== 核心模型创建视图（整合版） ==========
@login_required
@csrf_protect  # 强制CSRF保护
def model_create(request):
    """整合版模型创建视图 - 兼容前后端交互"""
    if request.method == 'GET':
        # 获取角色列表
        roles = Role.objects.all()
        return render(request, 'lowcode/model_create.html', {
            'roles': roles,
            'form': ModelForm(),
            'field_forms': [],
        })

    elif request.method == 'POST':
        try:
            # 1. 验证基础表单
            model_form = ModelForm(request.POST)
            if not model_form.is_valid():
                return JsonResponse({
                    'success': False,
                    'message': '基础信息验证失败',
                    'errors': model_form.errors
                }, status=400)

            # 2. 验证字段数据
            field_names = request.POST.getlist('field_names[]')
            field_types = request.POST.getlist('field_types[]')
            field_lengths = request.POST.getlist('field_lengths[]')
            field_required = request.POST.getlist('field_required[]')
            field_defaults = request.POST.getlist('field_defaults[]')
            field_comments = request.POST.getlist('field_comments[]')

            # 检查字段数据完整性
            if len(field_names) == 0:
                return JsonResponse({
                    'success': False,
                    'message': '至少需要添加一个字段'
                }, status=400)

            # 验证每个字段
            field_forms = []
            field_datas = []  # 存储字段数据用于后续创建
            for i in range(len(field_names)):
                field_data = {
                    'name': field_names[i],
                    'type': field_types[i] if i < len(field_types) else '',
                    'length': field_lengths[i] if i < len(field_lengths) else '',
                    'required': field_required[i] if i < len(field_required) else 'True',
                    'default': field_defaults[i] if i < len(field_defaults) else '',
                    'comment': field_comments[i] if i < len(field_comments) else '',
                }

                # 转换required为布尔值
                field_data['required'] = field_data['required'] == 'True'

                # 验证字符串类型长度必填
                if field_data['type'] in ['char', 'varchar'] and not field_data['length']:
                    return JsonResponse({
                        'success': False,
                        'message': f'字段「{field_data["name"]}」是字符串类型，必须填写长度'
                    }, status=400)

                field_form = FieldForm(field_data)
                if not field_form.is_valid():
                    return JsonResponse({
                        'success': False,
                        'message': f'字段「{field_data["name"]}」验证失败：{field_form.errors}'
                    }, status=400)
                field_forms.append(field_form)
                field_datas.append(field_data)

            # ========== 修复步骤2：模型唯一性校验（兼容已删除模型） ==========
            model_name = model_form.cleaned_data['name']
            # 检查数据库中是否存在
            db_exists = LowCodeModelConfig.objects.filter(name=model_name).exists()
            # 检查动态注册表中是否存在
            registry_exists = get_dynamic_model(model_name) is not None

            # 仅当数据库和注册表都存在时，才判定为已存在
            if db_exists and registry_exists:
                return JsonResponse({
                    'success': False,
                    'message': f'模型 {model_name} 已存在，请更换模型名称'
                }, status=400)
            # 如果仅注册表存在（数据库已删除），先清理注册表
            elif registry_exists and not db_exists:
                unregister_dynamic_model(model_name)
                logger.info(f"清理残留的动态模型：{model_name}")

            # 3. 事务内创建模型和字段
            with transaction.atomic():
                # 创建模型配置
                model_config = LowCodeModelConfig.objects.create(
                    name=model_name,
                    table_name=model_form.cleaned_data['table_name'],
                    description='',  # 可扩展添加描述字段
                    is_active=True
                )

                # 添加角色关联
                role_ids = model_form.cleaned_data['roles']
                if role_ids:
                    roles = Role.objects.filter(id__in=role_ids)
                    model_config.roles.set(roles)

                # 创建字段配置 - 使用修复后的函数
                for i, field_data in enumerate(field_datas):
                    create_field_with_default(model_config, field_data, i)

                # 同步到动态模型注册表
                model_config._skip_sync = False
                model_config.sync_to_dynamic_registry(create_table=True)

            # 清除缓存（兼容所有缓存后端）
            safe_clear_model_cache(model_config.name)

            # 4. 返回成功响应
            return JsonResponse({
                'success': True,
                'message': '模型创建成功',
                'redirect_url': reverse('lowcode:model-list')
            })

        except ValidationError as e:
            return JsonResponse({
                'success': False,
                'message': f'数据验证失败：{str(e)}'
            }, status=400)

        except Exception as e:
            # 记录详细错误日志
            logger.error(f'创建模型失败：{str(e)}', exc_info=True)

            return JsonResponse({
                'success': False,
                'message': f'服务器内部错误：{str(e)}' if settings.DEBUG else '服务器内部错误，请联系管理员'
            }, status=500)

    else:
        return JsonResponse({
            'success': False,
            'message': '不支持的请求方法'
        }, status=405)


# ========== 修复兼容旧版创建视图的布尔字段默认值 ==========
@csrf_exempt
@require_http_methods(["GET", "POST", "OPTIONS"])
def model_create_view(request: HttpRequest):
    """兼容旧版的模型创建视图"""
    if request.method == 'OPTIONS':
        response = HttpResponse()
        response['Access-Control-Allow-Origin'] = '*'
        response['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response['Access-Control-Allow-Headers'] = 'Content-Type, X-CSRFToken'
        return response

    if not request.user.is_superuser:
        if request.method == 'POST':
            return JsonResponse({'code': 403, 'msg': '无管理员权限'}, status=403)
        messages.error(request, "无管理员权限")
        return redirect('admin:index')

    if request.method == 'POST':
        try:
            # 兼容表单提交和JSON提交
            if request.content_type == 'application/json':
                data = json.loads(request.body)
            else:
                data = request.POST

            # 获取基础信息
            model_name = data.get('name')
            table_name = data.get('table_name')
            role_ids = data.get('roles', [])

            # 处理角色ID（兼容字符串和列表）
            if isinstance(role_ids, str):
                try:
                    role_ids = json.loads(role_ids)
                except:
                    role_ids = role_ids.split(',') if role_ids else []

            # ========== 新增：处理前端传递的字段数组 ==========
            # 从表单中获取字段相关的数组参数
            field_names = data.getlist('field_names[]') if not request.content_type == 'application/json' else data.get(
                'field_names', [])
            field_types = data.getlist('field_types[]') if not request.content_type == 'application/json' else data.get(
                'field_types', [])
            field_lengths = data.getlist(
                'field_lengths[]') if not request.content_type == 'application/json' else data.get('field_lengths', [])
            field_required = data.getlist(
                'field_required[]') if not request.content_type == 'application/json' else data.get('field_required',
                                                                                                    [])
            field_defaults = data.getlist(
                'field_defaults[]') if not request.content_type == 'application/json' else data.get('field_defaults',
                                                                                                    [])
            field_comments = data.getlist(
                'field_comments[]') if not request.content_type == 'application/json' else data.get('field_comments',
                                                                                                    [])

            # 验证基础信息
            if not model_name:
                return JsonResponse({'code': 400, 'msg': '模型名称不能为空'}, status=400)

            if not re.match(r'^[a-zA-Z][a-zA-Z0-9_]*$', model_name):
                return JsonResponse({'code': 400, 'msg': '模型名称必须以字母开头，仅含字母、数字、下划线'}, status=400)

            # ========== 修复步骤2：模型唯一性校验（兼容已删除模型） ==========
            # 检查数据库中是否存在
            db_exists = LowCodeModelConfig.objects.filter(name=model_name).exists()
            # 检查动态注册表中是否存在
            registry_exists = get_dynamic_model(model_name) is not None

            # ========== 修复：只要数据库中存在同名模型，就拒绝创建 ==========
            if LowCodeModelConfig.objects.filter(name=model_name).exists():
                return JsonResponse({
                    'code': 400,
                    'msg': f'模型名称 "{model_name}" 已被使用，请更换名称',
                    'success': False,
                    'message': f'模型名称 "{model_name}" 已被使用，请更换名称'
                }, status=400, json_dumps_params={'ensure_ascii': False})

            # 可选：如果注册表中有残留（但数据库无记录），清理它
            registry_exists = get_dynamic_model(model_name) is not None
            if registry_exists:
                unregister_dynamic_model(model_name)
                logger.info(f"清理残留的动态模型注册：{model_name}")

            if LowCodeModelConfig.objects.filter(table_name=table_name).exists():
                return JsonResponse({'code': 400, 'msg': f'数据表名 "{table_name}" 已被使用'}, status=400)

            # 构建字段列表（配对字段属性）
            fields = []
            max_len = max(len(field_names), len(field_types), len(field_required))
            for i in range(max_len):
                field_name = field_names[i] if i < len(field_names) else ''
                field_type = field_types[i] if i < len(field_types) else ''

                # 跳过空字段名称
                if not field_name or not field_type:
                    continue

                field_data = {
                    'name': field_name.strip(),
                    'type': field_type.strip(),
                    'length': field_lengths[i] if i < len(field_lengths) and field_lengths[i].strip() else None,
                    'required': field_required[i] == 'True' if i < len(field_required) else False,
                    'default': field_defaults[i] if i < len(field_defaults) and field_defaults[i].strip() else None,
                    'comment': field_comments[i] if i < len(field_comments) and field_comments[i].strip() else None
                }
                fields.append(field_data)

            # 验证字段
            valid_fields = [f for f in fields if f.get('name') and f.get('type')]
            if not valid_fields:
                return JsonResponse({'code': 400, 'msg': '至少需要一个有效字段'}, status=400)

            # ========== 修复核心：先跳过同步保存模型 ==========
            model_config = LowCodeModelConfig(
                name=model_name,
                table_name=table_name or f'lowcode_{model_name.lower()}'
            )
            # 设置跳过同步标记
            model_config._skip_sync = True
            # 保存模型（不触发同步）
            model_config.save()

            # 关联角色
            if role_ids and isinstance(role_ids, list):
                roles = Role.objects.filter(id__in=role_ids)
                model_config.roles.set(roles)

            # 创建字段记录 - 使用修复后的函数
            field_errors = []
            valid_field_count = 0  # 新增：统计有效字段数
            for idx, field_data in enumerate(valid_fields):
                try:
                    create_field_with_default(model_config, field_data, idx)
                    valid_field_count += 1  # 有效字段数+1
                except Exception as e:
                    field_errors.append(f"字段 {field_data['name']} 创建失败：{str(e)}")
                    logger.error(f"创建字段 {field_data['name']} 失败: {str(e)}")

            # ========== 修复核心：字段创建完成后手动触发同步 ==========
            # 取消跳过标记
            model_config._skip_sync = False
            # 手动同步（确保有字段才同步）
            if model_config.is_active and valid_field_count > 0:
                model_config.sync_to_dynamic_registry(create_table=True)
            else:
                logger.warning(f"模型 {model_name} 无有效字段，跳过同步")

            # 清除缓存（兼容所有缓存后端）
            safe_clear_model_cache(model_name)

            # 构建响应数据
            response_data = {
                'code': 200,
                'msg': f"模型 {model_name} 创建成功！",
                'data': {
                    'model_id': model_config.id,
                    'model_name': model_config.name,
                    'table_name': model_config.table_name,
                    'field_count': len(valid_fields) - len(field_errors)
                }
            }

            # 兼容前端的success返回格式
            if request.content_type != 'application/json':
                response_data['success'] = True
                response_data['message'] = response_data['msg']
                del response_data['code']
                del response_data['msg']

            if field_errors:
                response_data['warning'] = field_errors
                response_data['msg' if 'msg' in response_data else 'message'] += f"（{len(field_errors)}个字段创建失败）"

            return JsonResponse(response_data)

        except Exception as e:
            logger.exception("创建动态模型失败")
            error_response = {
                'code': 500,
                'msg': f'创建失败：{str(e)}',
                'error_detail': str(e) if settings.DEBUG else None
            }
            # 兼容前端的错误返回格式
            if request.content_type != 'application/json':
                error_response['success'] = False
                error_response['message'] = error_response['msg']
                del error_response['code']
                del error_response['msg']
            return JsonResponse(error_response, status=500)

    # GET请求：渲染创建页面
    form = LowCodeModelConfigForm()
    roles = Role.objects.all()
    context = {
        'form': form,
        'roles': roles,
        'title': '创建动态模型',
        'lowcode_base_url': request.build_absolute_uri('/lowcode/')
    }
    return render(request, 'lowcode/model-create.html', context)


# ========== 新增：手动创建 UserGroup25 模型的函数 ==========
@login_required
@permission_classes([IsAdminUser])
def create_usergroup25_model(request):
    """
    手动创建 UserGroup25 模型（包含 isactive 布尔字段，默认值 True）
    可通过访问 /lowcode/create-usergroup25/ 调用
    """
    if not request.user.is_superuser:
        return JsonResponse({'code': 403, 'msg': '仅管理员可执行此操作'}, status=403)

    try:
        model_name = 'UserGroup25'
        table_name = 'lowcode_usergroup25'

        # 检查模型是否已存在
        if LowCodeModelConfig.objects.filter(name=model_name).exists():
            return JsonResponse({
                'code': 400,
                'msg': f'模型 {model_name} 已存在',
                'success': False
            }, status=400)

        # 事务内创建模型和字段
        with transaction.atomic():
            # 创建模型配置
            model_config = LowCodeModelConfig.objects.create(
                name=model_name,
                table_name=table_name,
                description='用户组25模型',
                is_active=True
            )

            # 创建 isactive 布尔字段（默认值 True）
            field_data = {
                'name': 'isactive',
                'type': 'boolean',
                'required': True,
                'default': True,
                'comment': '是否激活'
            }
            create_field_with_default(model_config, field_data, 0)

            # 同步表结构
            model_config._skip_sync = False
            model_config.sync_to_dynamic_registry(create_table=True)

        # 清理缓存
        safe_clear_model_cache(model_name)

        logger.info(f"UserGroup25 模型创建成功，包含 isactive 布尔字段（默认值 True）")

        return JsonResponse({
            'code': 200,
            'msg': 'UserGroup25 模型创建成功',
            'success': True,
            'data': {
                'model_name': model_name,
                'table_name': table_name,
                'fields': [
                    {
                        'name': 'isactive',
                        'type': 'boolean',
                        'default': True,
                        'comment': '是否激活'
                    }
                ]
            }
        })

    except Exception as e:
        logger.error(f"创建 UserGroup25 模型失败：{str(e)}", exc_info=True)
        return JsonResponse({
            'code': 500,
            'msg': f'创建失败：{str(e)}',
            'success': False
        }, status=500)


# ========== 原有函数视图 ==========
def dynamic_model_detail(request: HttpRequest, model_name: str):
    try:
        model_class = get_model_by_name(model_name)
        model_config = LowCodeModelConfig.objects.filter(name=model_name).first()

        field_details = []
        for field in model_class._meta.fields:
            field_details.append({
                'name': field.name,
                'verbose_name': field.verbose_name or field.name,
                'field_type': field.__class__.__name__,
                'blank': field.blank,
                'null': field.null,
                'default': field.default if field.default is not models.NOT_PROVIDED else None,
                'max_length': getattr(field, 'max_length', None),
                'choices': dict(field.choices) if field.choices else None,
                'related_model': field.related_model.__name__ if hasattr(field, 'related_model') else None,
            })

        context = {
            'title': f'{model_class._meta.verbose_name} 配置详情',
            'model_name': model_name,
            'model_class': model_class,
            'model_config': model_config,
            'meta': model_class._meta,
            'field_details': field_details,
            'record_count': get_model_record_count(model_name),
            'table_name': model_class._meta.db_table,
        }
        return render(request, 'lowcode/dynamic_model_config_detail.html', context)
    except Http404 as e:
        messages.error(request, str(e))
        return redirect('lowcode:model-list')
    except Exception as e:
        logger.exception(f"加载模型配置详情异常：{model_name}")
        messages.error(request, f"加载模型详情失败：{str(e)}")
        return redirect('lowcode:model-list')


def dynamic_model_data(request: HttpRequest, model_name: str):
    try:
        list_view = DynamicModelListView()
        list_view.setup(request, model_name=model_name)
        queryset = list_view.get_queryset()

        paginator = Paginator(queryset, list_view.paginate_by)
        page = request.GET.get('page', 1)
        try:
            object_list = paginator.page(page)
        except PageNotAnInteger:
            object_list = paginator.page(1)
        except EmptyPage:
            object_list = paginator.page(paginator.num_pages)

        context = {
            'title': f'{list_view.model_class._meta.verbose_name} 数据列表',
            'model_name': model_name,
            'object_list': object_list,
            'paginator': paginator,
            'page_obj': object_list,
            'is_paginated': object_list.has_other_pages(),
            'search_query': request.GET.get('search', ''),
            'verbose_name': list_view.model_class._meta.verbose_name,
            'verbose_name_plural': list_view.model_class._meta.verbose_name_plural,
        }
        return render(request, 'lowcode/dynamic_model_data_list.html', context)
    except Http404 as e:
        messages.error(request, str(e))
        return redirect('lowcode:model-list')
    except Exception as e:
        logger.exception(f"加载模型数据列表异常：{model_name}")
        messages.error(request, f"加载数据列表失败：{str(e)}")
        return redirect('lowcode:model-list')


def model_upgrade_view(request: HttpRequest):
    if not request.user.is_superuser:
        messages.error(request, "无权限访问")
        return redirect('lowcode:model-list')
    return render(request, 'lowcode/model_upgrade.html', {
        'title': '模型升级',
        'lowcode_base_url': request.build_absolute_uri('/lowcode/')
    })


def model_list_view(request: HttpRequest):
    if not request.user.is_superuser:
        messages.error(request, "无权限访问")
        return redirect('admin:index')
    try:
        model_configs = get_all_dynamic_model_configs()
        return render(request, 'lowcode/model_list.html', {
            'model_configs': model_configs,
            'title': '动态模型列表',
        })
    except Exception as e:
        logger.exception("动态模型列表加载失败")
        messages.error(request, "系统异常，请联系管理员")
        return render(request, 'lowcode/model_list.html', {
            'model_configs': [],
            'title': '动态模型列表',
        })


@login_required
@csrf_protect
def model_delete_view(request: HttpRequest, model_name: str):
    """删除整个低代码模型（包括配置、字段、数据库表、动态注册）"""
    if not request.user.is_superuser:
        messages.error(request, "无管理员权限")
        return redirect('lowcode:model-list')

    # 获取模型配置
    model_config = get_object_or_404(LowCodeModelConfig, name=model_name)

    if request.method == 'POST':
        try:
            table_name = model_config.table_name or f'lowcode_{model_name.lower()}'

            # 1. 删除字段配置
            FieldModel.objects.filter(model_config=model_config).delete()

            # 2. 从动态注册表中注销
            unregister_dynamic_model(model_name)
            logger.info(f"已注销动态模型：{model_name}")

            # 3. 删除数据库表（使用 schema_editor，兼容所有数据库）
            from django.db import connection

            # 动态创建一个临时模型类，仅用于删除表
            class TempModel(models.Model):
                class Meta:
                    db_table = table_name
                    app_label = 'lowcode'
                    managed = False  # 关键：避免 Django 认为这是需要迁移的模型

            try:
                with connection.schema_editor() as schema_editor:
                    schema_editor.delete_model(TempModel)
                logger.info(f"已成功删除数据库表：{table_name}")
            except Exception as e:
                # 表可能已被手动删除，或不存在，记录警告但不中断流程
                logger.warning(f"删除数据库表 {table_name} 时出错（可能已不存在）: {e}")

            # 4. 删除主配置
            model_config.delete()

            # 5. 清理缓存
            safe_clear_model_cache(model_name)

            messages.success(request, f'模型 “{model_name}” 已成功删除！')
            return redirect('lowcode:model-list')

        except Exception as e:
            logger.exception(f"删除模型 {model_name} 失败")
            messages.error(request, f"删除失败：{str(e)}")
            return redirect('lowcode:model-delete', model_name=model_name)

    # GET 请求：确认页面
    context = {
        'title': f'确认删除模型 “{model_name}”',
        'model_name': model_name,
        'model_config': model_config,
    }
    return render(request, 'lowcode/model_confirm_delete.html', context)


# ========== 类视图 ==========
class DynamicModelListView(LoginRequiredMixin, ListView):
    template_name = 'lowcode/dynamic_model_list.html'
    context_object_name = 'object_list'
    paginate_by = 20

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.model_name = self.kwargs['model_name']
        self.model_class = get_model_by_name(self.model_name)

    def get_queryset(self):
        queryset = self.model_class.objects.all()
        search_query = self.request.GET.get('search', '')
        if search_query:
            search_filters = Q()
            for field in self.model_class._meta.fields:
                if isinstance(field, (models.CharField, models.TextField, models.EmailField, models.URLField)):
                    search_filters |= Q(**{f'{field.name}__icontains': search_query})
            queryset = queryset.filter(search_filters)
        return queryset.order_by('-id')

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        meta = self.model_class._meta
        context.update({
            'title': f'{meta.verbose_name} 列表',
            'model_name': self.model_name,
            'verbose_name': meta.verbose_name,
            'verbose_name_plural': meta.verbose_name_plural,
            'search_query': self.request.GET.get('search', ''),
        })
        return context


class DynamicModelCreateView(LoginRequiredMixin, CreateView):
    template_name = 'lowcode/dynamic_model_form.html'

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.model_name = self.kwargs['model_name']
        self.model_class = get_model_by_name(self.model_name)
        self.form_class = build_dynamic_form(self.model_class)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        enhanced_fields = enhance_form_fields(context['form'], self.model_class)
        context.update({
            'title': f'新增 {self.model_class._meta.verbose_name}',
            'model_name': self.model_name,
            'enhanced_fields': enhanced_fields,
            'has_ckeditor': any(f['widget_type'] == 'ckeditor' for f in enhanced_fields),
            'action': 'create',
        })
        return context

    def get_success_url(self):
        messages.success(self.request, f'{self.model_class._meta.verbose_name} 创建成功！')
        safe_clear_model_cache(self.model_name)
        return reverse('lowcode:dynamic-model-list', kwargs={'model_name': self.model_name})


class DynamicModelDetailView(LoginRequiredMixin, DetailView):
    template_name = 'lowcode/dynamic_model_detail.html'

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.model_name = self.kwargs['model_name']
        self.model_class = get_model_by_name(self.model_name)

    def get_object(self, queryset=None):
        return get_object_or_404(self.model_class, pk=self.kwargs['pk'])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        enhanced_fields = []
        for field in self.model_class._meta.fields:
            value = getattr(self.object, field.name)
            display_value = getattr(self.object, f'get_{field.name}_display')() if field.choices else value

            if isinstance(field, models.FileField) and value:
                display_value = f'<a href="{value.url}" target="_blank">下载文件</a>'
            elif isinstance(field, models.ImageField) and value:
                display_value = f'<img src="{value.url}" style="max-width: 200px; max-height: 200px;" alt="预览图">'
            elif isinstance(field, (models.DateField, models.DateTimeField)) and value:
                display_value = value.strftime('%Y-%m-%d %H:%M:%S') if isinstance(field,
                                                                                  models.DateTimeField) else value.strftime(
                    '%Y-%m-%d')

            widget_type = 'image' if isinstance(field, models.ImageField) else \
                'file' if isinstance(field, models.FileField) else \
                    'ckeditor' if isinstance(field, models.TextField) else \
                        'boolean' if isinstance(field, models.BooleanField) else \
                            'select' if field.choices else 'default'

            enhanced_fields.append({
                'label': field.verbose_name or field.name,
                'value': value,
                'display_value': display_value,
                'widget_type': widget_type,
                'field_type': field.__class__.__name__,
            })

        context.update({
            'title': f'查看 {self.model_class._meta.verbose_name}',
            'model_name': self.model_name,
            'enhanced_fields': enhanced_fields,
        })
        return context


class DynamicModelUpdateView(LoginRequiredMixin, UpdateView):
    template_name = 'lowcode/dynamic_model_form.html'

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.model_name = self.kwargs['model_name']
        self.model_class = get_model_by_name(self.model_name)
        self.form_class = build_dynamic_form(self.model_class)

    def get_object(self, queryset=None):
        return get_object_or_404(self.model_class, pk=self.kwargs['pk'])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        enhanced_fields = enhance_form_fields(context['form'], self.model_class)
        context.update({
            'title': f'编辑 {self.model_class._meta.verbose_name}',
            'model_name': self.model_name,
            'enhanced_fields': enhanced_fields,
            'has_ckeditor': any(f['widget_type'] == 'ckeditor' for f in enhanced_fields),
            'action': 'update',
            'object_id': self.object.pk,
        })
        return context

    def get_success_url(self):
        messages.success(self.request, f'{self.model_class._meta.verbose_name} 更新成功！')
        return reverse('lowcode:dynamic-model-detail', kwargs={'model_name': self.model_name, 'pk': self.object.pk})


class DynamicModelDeleteView(LoginRequiredMixin, DeleteView):
    template_name = 'lowcode/dynamic_model_confirm_delete.html'

    def setup(self, request, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.model_name = self.kwargs['model_name']
        self.model_class = get_model_by_name(self.model_name)

    def get_object(self, queryset=None):
        return get_object_or_404(self.model_class, pk=self.kwargs['pk'])

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context.update({
            'title': f'确认删除 {self.model_class._meta.verbose_name}',
            'model_name': self.model_name,
            'verbose_name': self.model_class._meta.verbose_name,
            'object_name': str(self.object)[:50],
        })
        return context

    def get_success_url(self):
        messages.success(self.request, f'{self.model_class._meta.verbose_name} 已删除。')
        safe_clear_model_cache(self.model_name)
        return reverse('lowcode:dynamic-model-list', kwargs={'model_name': self.model_name})


# ========== DRF API 视图 ==========
@api_view(['POST'])
@permission_classes([IsAdminUser])
def create_model_api(request):
    try:
        name = request.data.get('name')
        role_ids = request.data.get('roles', [])
        fields = request.data.get('fields', [])

        if not name:
            return Response({'success': False, 'message': '模型名称不能为空'}, status=status.HTTP_400_BAD_REQUEST)

        if not re.match(r'^[a-zA-Z][a-zA-Z0-9_]*$', name):
            return Response({'success': False, 'message': '模型名称格式不合法'}, status=status.HTTP_400_BAD_REQUEST)

        # ========== 修复步骤2：模型唯一性校验（兼容已删除模型） ==========
        db_exists = LowCodeModelConfig.objects.filter(name=name).exists()
        registry_exists = get_dynamic_model(name) is not None

        if db_exists and registry_exists:
            return Response({'success': False, 'message': f'模型 {name} 已存在'}, status=status.HTTP_400_BAD_REQUEST)
        elif registry_exists and not db_exists:
            unregister_dynamic_model(name)
            logger.info(f"清理残留的动态模型：{name}")

        # 创建模型配置（跳过同步）
        model = LowCodeModelConfig(
            name=name,
            table_name=f'lowcode_{name.lower()}'
        )
        model._skip_sync = True
        model.save()

        if role_ids:
            roles = Role.objects.filter(id__in=role_ids)
            model.roles.set(roles)

        # 创建字段记录 - 使用修复后的函数
        for idx, field in enumerate(fields):
            if not field.get('name') or not field.get('type'):
                continue
            create_field_with_default(model, field, idx)

        # 字段创建完成后手动同步
        model._skip_sync = False
        if model.is_active:
            model.sync_to_dynamic_registry(create_table=True)

        # 清除缓存（兼容所有缓存后端）
        safe_clear_model_cache(name)

        return Response({
            'success': True,
            'message': f'模型 {name} 创建成功',
            'data': {'model_id': model.id, 'table_name': model.table_name}
        })
    except Exception as e:
        logger.exception("API 创建模型失败")
        return Response({
            'success': False,
            'message': str(e),
            'error_detail': str(e) if settings.DEBUG else None
        }, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def get_role_list_api(request):
    roles = Role.objects.all().values('id', 'name', 'code')
    return Response({'success': True, 'data': list(roles)})


@api_view(['GET'])
@permission_classes([IsAdminUser])
def check_table_exists_api(request):
    table_name = request.query_params.get('table_name')
    if not table_name:
        return Response({'success': False, 'message': '表名不能为空'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        with connection.cursor() as cursor:
            vendor = connection.vendor
            if vendor == 'postgresql':
                cursor.execute("SELECT to_regclass(%s)", [table_name])
                exists = cursor.fetchone()[0] is not None
            elif vendor in ('mysql', 'mariadb'):
                db_name = connection.settings_dict['NAME']
                cursor.execute(
                    "SELECT 1 FROM information_schema.tables WHERE table_schema = %s AND table_name = %s",
                    [db_name, table_name]
                )
                exists = cursor.fetchone() is not None
            else:  # SQLite
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=%s;", [table_name])
                exists = cursor.fetchone() is not None

        return Response({'success': True, 'data': exists})
    except Exception as e:
        logger.exception(f"检测表是否存在失败：{table_name}")
        return Response({'success': False, 'message': str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


# ========== 动态方法视图（非 DRF，手动权限校验） ==========
@require_http_methods(["POST", "GET"])
def refresh_methods(request: HttpRequest):
    if not request.user.is_superuser:
        return JsonResponse({"code": 403, "msg": "无权限"}, status=403)

    try:
        if request.method == 'POST' and request.content_type == 'application/json':
            data = json.loads(request.body)
            model_name = data.get('model_name')
        else:
            model_name = request.GET.get('model_name')

        if not model_name:
            return JsonResponse({"code": 400, "msg": "缺少模型名称参数"}, status=400)

        refresh_dynamic_methods(model_name)

        # 清除缓存（兼容所有缓存后端）
        safe_clear_model_cache(model_name)

        logger.info(f"模型 {model_name} 动态方法刷新成功")

        if request.method == 'POST':
            return JsonResponse({"code": 200, "msg": f"模型 {model_name} 动态方法刷新成功"})
        messages.success(request, f"模型 {model_name} 动态方法刷新成功")
        return redirect('lowcode:model-list')
    except Exception as e:
        logger.exception("刷新动态方法失败")
        if request.method == 'POST':
            return JsonResponse({"code": 500, "msg": f"刷新失败：{str(e)}"}, status=500)
        messages.error(request, f"刷新失败：{str(e)}")
        return redirect('lowcode:model-list')


@require_http_methods(["POST", "GET"])
def call_dynamic_method(request: HttpRequest, model_name: str, instance_id: int, method_name: str):
    if not request.user.is_superuser:
        return JsonResponse({"code": 403, "msg": "无权限"}, status=403)

    try:
        DynamicModel = get_dynamic_model_with_methods(model_name)
        if DynamicModel is None:
            raise Http404("模型不存在")

        instance = get_object_or_404(DynamicModel, id=instance_id)
        method = getattr(instance, method_name, None)

        if not method or not callable(method):
            raise Http404(f"方法 '{method_name}' 不存在")

        logger.info(f"用户 {request.user.username} 调用 {model_name}.{method_name} on {instance_id}")

        params = {}
        if request.method == 'POST':
            if request.content_type == 'application/json':
                params = json.loads(request.body)
            else:
                params = request.POST.dict()

        try:
            result = method(user=request.user, **params)
        except TypeError:
            try:
                result = method(**params)
            except TypeError:
                result = method()

        if isinstance(result, (dict, list, str, int, float, bool, type(None))):
            response_data = result
        else:
            response_data = str(result)

        return JsonResponse({
            "code": 200,
            "msg": "方法调用成功",
            "data": {"result": response_data}
        })
    except Http404 as e:
        return JsonResponse({"code": 404, "msg": str(e)}, status=404)
    except Exception as e:
        logger.exception(f"调用动态方法异常：{model_name}.{method_name}")
        return JsonResponse({
            "code": 500,
            "msg": f"调用失败：{str(e)}",
            "error_detail": str(e) if settings.DEBUG else None
        }, status=500)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def check_model_name_api(request):
    """检查模型名称是否已存在"""
    model_name = request.query_params.get('name')
    if not model_name:
        return Response({'success': False, 'message': '模型名称不能为空'}, status=400)

    # ========== 修复步骤2：模型唯一性校验（兼容已删除模型） ==========
    db_exists = LowCodeModelConfig.objects.filter(name=model_name).exists()
    registry_exists = get_dynamic_model(model_name) is not None

    # 仅当数据库和注册表都存在时，才返回存在
    exists = db_exists and registry_exists
    # 如果仅注册表存在，清理后返回不存在
    if registry_exists and not db_exists:
        unregister_dynamic_model(model_name)
        logger.info(f"清理残留的动态模型：{model_name}")
        exists = False

    return Response({'success': True, 'data': exists})


def unregister_dynamic_model(model_name: str, clear_cache: bool = True, delete_table: bool = False):
    """
    注销动态模型
    :param model_name: 模型名称
    :param clear_cache: 是否清理缓存
    :param delete_table: 是否删除数据表和数据
    """
    # 1. 注销模型（从apps中移除）
    app_label = 'lowcode'  # 修复：改为正确的app标签
    model_key = f'{app_label}.{model_name.lower()}'
    if app_label in apps.all_models and model_name.lower() in apps.all_models[app_label]:
        del apps.all_models[app_label][model_name.lower()]

    # 2. 清理缓存（如果需要）
    if clear_cache:
        safe_clear_model_cache(model_name)

    # 3. 删除数据表（核心逻辑）
    if delete_table:
        table_name = f'lowcode_{model_name.lower()}'  # 修复：使用正确的表名规则
        with connection.cursor() as cursor:
            # 执行删表SQL（注意：生产环境需谨慎，建议增加二次确认）
            cursor.execute(f'DROP TABLE IF EXISTS {connection.ops.quote_name(table_name)};')
            logger.info(f'数据表 {table_name} 已删除（含所有数据）')


# ========== 原有：仅删除模型配置（保留数据表） ==========
@login_required
@csrf_protect
@require_POST
def model_config_delete_view(request: HttpRequest):
    """删除动态模型配置（仅删除配置，不删除数据表和数据）"""
    if not request.user.is_superuser:
        return JsonResponse({
            'code': 403,
            'msg': '无管理员权限'
        }, status=403)

    try:
        # 获取请求数据
        data = json.loads(request.body)
        model_name = data.get('model_name')
        model_id = data.get('model_id')

        if not model_name:
            return JsonResponse({
                'code': 400,
                'msg': '模型名称不能为空'
            }, status=400)

        # 获取模型配置
        if model_id:
            model_config = get_object_or_404(LowCodeModelConfig, id=model_id, name=model_name)
        else:
            model_config = get_object_or_404(LowCodeModelConfig, name=model_name)

        # 事务内删除配置
        with transaction.atomic():
            # 1. 删除字段配置
            FieldModel.objects.filter(model_config=model_config).delete()

            # 2. 从动态注册表中注销（保留数据表）
            unregister_dynamic_model(model_name, clear_cache=True, delete_table=False)

            # 3. 删除主配置记录
            model_config.delete()

            # 4. 清理缓存
            safe_clear_model_cache(model_name)

        logger.info(f'用户 {request.user.username} 删除了模型 {model_name} 的配置（保留数据表）')

        return JsonResponse({
            'code': 200,
            'msg': f'模型 {model_name} 配置已成功删除（数据表未删除）'
        })

    except Http404:
        return JsonResponse({
            'code': 404,
            'msg': f'模型 {model_name} 配置不存在'
        }, status=404)
    except Exception as e:
        logger.exception(f"删除模型 {model_name} 配置失败")
        return JsonResponse({
            'code': 500,
            'msg': f'删除失败：{str(e)}'
        }, status=500)


# ========== 调整后：删除模型（含配置+数据表+数据） ==========
@login_required
@csrf_protect
@require_POST
def model_delete(request, model_name):
    """删除动态模型（含数据表和数据）"""
    # 1. 校验请求方法
    if request.method != 'POST':
        return JsonResponse({
            'code': 405,
            'msg': '仅支持POST请求'
        }, status=405)

    # 2. 校验模型名称
    if not model_name or model_name.strip() == '':
        return JsonResponse({
            'code': 400,
            'msg': '模型名称不能为空'
        }, status=400)

    model_name = model_name.strip()

    # 3. 权限校验
    if not request.user.is_superuser:
        return JsonResponse({
            'code': 403,
            'msg': '仅管理员可执行此操作'
        }, status=403)

    try:
        # 4. 查询模型配置
        model_config = LowCodeModelConfig.objects.filter(name=model_name).first()
        if not model_config:
            return JsonResponse({
                'code': 404,
                'msg': f'模型 {model_name} 不存在'
            }, status=404)

        # 5. 安全删除数据表
        table_name = model_config.table_name or f'lowcode_{model_name.lower()}'
        with connection.cursor() as cursor:
            # 防止SQL注入，安全引用表名
            cursor.execute(f"DROP TABLE IF EXISTS {connection.ops.quote_name(table_name)};")

        # 6. 删除字段配置
        FieldModel.objects.filter(model_config=model_config).delete()

        # 7. 从动态注册表中注销
        unregister_dynamic_model(model_name, delete_table=False)

        # 8. 删除模型配置记录
        model_config.delete()

        # 9. 清理缓存
        safe_clear_model_cache(model_name)

        # 10. 记录操作日志
        logger.info(f'管理员 {request.user.username} 成功删除模型：{model_name}（数据表：{table_name}）')

        return JsonResponse({
            'code': 200,
            'msg': f'模型 {model_name} 已彻底删除（含数据表 {table_name}）'
        })

    except Exception as e:
        # 11. 记录错误日志
        logger.error(f'删除模型 {model_name} 失败：{str(e)}', exc_info=True)

        # 12. 返回友好错误信息
        error_msg = str(e) if settings.DEBUG else '删除失败：服务器内部错误'

        return JsonResponse({
            'code': 500,
            'msg': error_msg
        }, status=500)


# ========== 兼容类视图（可选） ==========
from django.views import View
from django.utils.decorators import method_decorator


@method_decorator(login_required, name='dispatch')
@method_decorator(csrf_protect, name='dispatch')
class ModelConfigDeleteView(View):
    """删除模型配置的类视图（仅删配置，不删表）"""

    def post(self, request: HttpRequest):
        return model_config_delete_view(request)


@method_decorator(login_required, name='dispatch')
@method_decorator(csrf_protect, name='dispatch')

class ModelDeleteView(View):
    """彻底删除模型的类视图（含表和数据）"""

    def post(self, request: HttpRequest, model_name):
        return model_delete(request, model_name)