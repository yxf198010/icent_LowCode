# lowcode/views.py
import logging
import os
import re
import json
from datetime import datetime, date
from uuid import uuid4
from typing import Any, Dict, List, Type, Optional
from django.apps import apps
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection, models
from django.db.models import Q
from django.http import Http404, HttpResponse, FileResponse, JsonResponse, HttpRequest
from django.shortcuts import render, get_object_or_404
from django.urls import reverse
from django.views.generic import ListView, CreateView, DetailView, UpdateView, DeleteView
from django import forms
from django.contrib.admin.views.decorators import staff_member_required
from django.core.cache import cache
from django.utils.text import capfirst
from django.utils import timezone
from django.template.response import TemplateResponse  # 修正：正确导入 TemplateResponse
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from rest_framework.viewsets import ReadOnlyModelViewSet
from rest_framework.filters import SearchFilter, OrderingFilter
from rest_framework.pagination import PageNumberPagination
from django_filters.rest_framework import DjangoFilterBackend
from rest_framework.permissions import IsAdminUser, IsAuthenticated
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.core.files.storage import default_storage
from lowcode.utils.vite import get_vite_asset

# Local imports
from .forms import ModelLowCodeForm
from .models.models import DataPermission, LowCodeMethodCallLog, ModelLowCode, FieldModel, LowCodeUser, Role
from .models.dynamic_model_factory import (
    refresh_dynamic_methods,
    get_dynamic_model_with_methods,
    get_dynamic_model,
    list_dynamic_model_names,
    ensure_dynamic_models_loaded
)
from .api.serializers import (
    LowCodeMethodCallLogSerializer,
    BatchDataPermissionSerializer,
    BatchRevokeDataPermissionSerializer,
)
from lowcode.io.excel import generate_method_log_excel
from .tasks import async_export_method_log

# Config
ALLOWED_MODELS = getattr(settings, 'LOWCODE_ALLOWED_MODELS', None)
EXPORT_MAX_RECORDS = getattr(settings, 'LOWCODE_EXPORT_MAX_RECORDS', 50000)
EXPORT_SUBDIR = getattr(settings, 'LOWCODE_EXPORT_SUBDIR', 'lowcode_exports/').rstrip('/') + '/'
CACHE_TIMEOUT = 60  # 统一缓存超时时间

logger = logging.getLogger(__name__)

# ========== Type Hints ==========
ModelConfigType = Dict[str, Any]
OverviewDataType = Dict[str, Any]


# ========== Utilities ==========
def get_model_by_name(model_name: str) -> Type[models.Model]:
    """根据模型名称获取动态模型类（增强错误处理）"""
    model_class = get_dynamic_model(model_name)
    if model_class is None:
        logger.warning(f"模型 '{model_name}' 不存在或未注册")
        raise Http404(f"模型 '{model_name}' 不存在或未注册")
    return model_class


def _sanitize_filename(filename: str) -> str:
    """清理文件名，避免非法字符"""
    if not re.match(r'^[a-zA-Z0-9._\-]+$', filename):
        raise ValidationError("文件名仅允许字母、数字、下划线、连字符和点")
    return filename


def _apply_log_filters(queryset, params: Dict[str, Any]):
    """应用日志过滤条件（优化过滤逻辑）"""
    filters = Q()
    if params.get("user"):
        filters &= Q(user_id=params["user"])
    if params.get("model_name"):
        filters &= Q(model_name__icontains=params["model_name"])
    if params.get("method_name"):
        filters &= Q(method_name__icontains=params["method_name"])
    if params.get("result_status"):
        filters &= Q(result_status=params["result_status"])
    if params.get("start_time"):
        filters &= Q(call_time__gte=params["start_time"])
    if params.get("end_time"):
        filters &= Q(call_time__lte=params["end_time"])
    return queryset.filter(filters) if filters else queryset


def get_model_record_count(model_name: str) -> int:
    """获取模型记录数（带缓存，优化性能）"""
    key = f'lowcode_data_count_{model_name}'
    cnt = cache.get(key)

    if cnt is None:
        try:
            model_class = get_dynamic_model(model_name)
            cnt = model_class.objects.count() if model_class else -1
            cache.set(key, cnt, CACHE_TIMEOUT)
        except Exception as e:
            logger.error(f"统计模型 '{model_name}' 记录数失败: {str(e)}")
            cnt = -1

    return cnt


def build_dynamic_form(model_class: Type[models.Model]) -> Type[forms.ModelForm]:
    """构建动态模型表单"""
    meta_attrs = {'model': model_class, 'fields': '__all__'}
    form_class = type(
        f'{model_class.__name__}Form',
        (forms.ModelForm,),
        {'Meta': type('Meta', (), meta_attrs)}
    )
    return configure_form_fields(form_class, model_class)


def configure_form_fields(form_class, model: models.Model):
    """配置表单字段属性和验证规则（优化代码结构）"""
    model_fields = {f.name: f for f in model._meta.get_fields() if hasattr(f, 'name')}
    form_fields = form_class.base_fields

    def get_base_attrs(label: str, model_field: models.Field) -> Dict[str, Any]:
        """获取基础字段属性"""
        return {
            'class': 'form-control',
            'placeholder': f'请输入{label}',
            'required': not (model_field.blank or model_field.null),
        }

    # 字段类型映射配置
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
                'min': mf.min_value if hasattr(mf, 'min_value') else None,
                'max': mf.max_value if hasattr(mf, 'max_value') else None,
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
            'widget': lambda attrs: forms.FileInput(attrs={**attrs, 'accept': 'image/*'}) if isinstance(model_field,
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

        # 应用字段类型配置
        for field_types, config in field_type_config.items():
            if isinstance(model_field, field_types):
                # 设置widget
                if 'widget' in config:
                    if callable(config['widget']):
                        form_field.widget = config['widget'](base_attrs)
                    else:
                        form_field.widget = config['widget'](attrs=base_attrs)

                # 修改attrs
                if 'attrs_modifier' in config and callable(config['attrs_modifier']):
                    form_field.widget.attrs = config['attrs_modifier'](model_field, form_field.widget.attrs)

                # 设置initial
                if 'initial' in config and callable(config['initial']) and form_field.initial is None:
                    initial_value = config['initial'](model_field)
                    if initial_value is not None:
                        form_field.initial = initial_value

                # 设置queryset
                if 'queryset' in config and callable(config['queryset']):
                    form_field.queryset = config['queryset'](model_field)

                break
        else:
            # 默认配置
            form_field.widget.attrs.update(base_attrs)

    # 自定义clean方法
    original_clean = getattr(form_class, 'clean', lambda self: super(type(self), self).clean())

    def enhanced_clean(self):
        cleaned_data = original_clean(self)
        for field_name, value in cleaned_data.items():
            model_field = model_fields.get(field_name)
            if not model_field:
                continue
            label = self[field_name].label or field_name

            # 字符串字段去空格
            if isinstance(model_field, (models.CharField, models.TextField)) and isinstance(value, str):
                cleaned_data[field_name] = value.strip()
                if base_attrs['required'] and not cleaned_data[field_name]:
                    self.add_error(field_name, f'{label}不能为空')

            # 日期时间验证
            elif isinstance(model_field, models.DateField) and value and value > date.today():
                self.add_error(field_name, f'{label}不能晚于当前日期')
            elif isinstance(model_field, models.DateTimeField) and value and value > timezone.now():
                self.add_error(field_name, f'{label}不能晚于当前时间')

            # 数值范围验证
            elif isinstance(model_field,
                            (models.IntegerField, models.FloatField, models.DecimalField)) and value is not None:
                if hasattr(model_field, 'min_value') and value < model_field.min_value:
                    self.add_error(field_name, f'{label}不能小于{model_field.min_value}')
                if hasattr(model_field, 'max_value') and value > model_field.max_value:
                    self.add_error(field_name, f'{label}不能大于{model_field.max_value}')

            # 文件大小验证
            elif isinstance(model_field, (models.FileField, models.ImageField)) and value and hasattr(value, 'size'):
                max_size = 10 * 1024 * 1024  # 10MB
                if value.size > max_size:
                    self.add_error(field_name, f'{label}大小不能超过10MB（当前{value.size // 1024 // 1024}MB）')

        return cleaned_data

    form_class.clean = enhanced_clean
    return form_class


def enhance_form_fields(form, model_class: Type[models.Model]) -> List[Dict[str, Any]]:
    """增强表单字段信息，用于模板渲染"""
    fields_info = []
    for field_name, bound_field in form.fields.items():
        model_field = model_class._meta.get_field(field_name)
        widget = bound_field.widget

        # 确定字段类型
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
            'label': bound_field.label or field_name,
            'bound_field': form[field_name],
            'widget_type': widget_type,
            'help_text': getattr(bound_field, 'help_text', ''),
            'required': bound_field.required
        })
    return fields_info


def get_all_dynamic_model_configs() -> List[ModelConfigType]:
    """获取所有动态模型配置（复用逻辑，避免代码冗余）"""
    ensure_dynamic_models_loaded()
    existing_tables = {t.lower() for t in connection.introspection.table_names()}
    model_names = list_dynamic_model_names()

    # 批量查询配置，减少数据库查询
    configs_queryset = ModelLowCode.objects.filter(name__in=model_names).only(
        'id', 'name', 'table_name', 'create_time', 'update_time'
    )
    config_map = {cfg.name: cfg for cfg in configs_queryset}

    model_configs = []
    for name in model_names:
        config = config_map.get(name)
        table = (config.table_name if config and config.table_name else name).lower()

        # 计算字段数（带缓存）
        field_count_key = f'lowcode_field_count_{name}'
        field_count = cache.get(field_count_key)
        if field_count is None:
            field_count = config.config.fields.count() if (config and hasattr(config, 'config')) else 0
            cache.set(field_count_key, field_count, CACHE_TIMEOUT)

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
            # 状态中文映射（用于模板显示）
            'status_cn': '已配置' if config and config.config.fields.count() > 0 else '未配置',
            'status_type': 'success' if config and config.config.fields.count() > 0 else 'secondary'
        })

    # 按创建时间倒序（最新的模型在前）
    return sorted(model_configs, key=lambda x: x['create_time'] or datetime.min, reverse=True)


def designer_view(request):
    context = {}
    if not (settings.DEBUG and getattr(settings, 'VITE_DEV_MODE', True)):
        try:
            context['vite_assets'] = get_vite_asset(app_name="lowcode_designer")
        except Exception as e:
            logger.error("Vite asset loading failed", exc_info=True)
            if settings.DEBUG:
                raise
    return render(request, 'lowcode/designer.html', context)


# ========== Views ==========
def index_view(request: HttpRequest):
    """首页视图（优化版，对应 index.html 模板）
    功能：显示动态模型统计、最近模型列表、快速操作入口所需数据
    """
    try:
        # 获取所有模型配置（带缓存优化）
        model_configs = get_all_dynamic_model_configs()

        # 计算首页统计数据（用于数据概览区域）
        total_models = len(model_configs)
        created_table_count = sum(1 for cfg in model_configs if cfg['exists_in_db'])
        total_fields = sum(cfg['field_count'] for cfg in model_configs)
        total_records = sum(cfg['record_count'] for cfg in model_configs if cfg['record_count'] != -1)

        # 组装上下文数据（适配 index.html 模板需求）
        context = {
            'model_configs': model_configs,
            'current_time': timezone.now(),
            'total_models': total_models,
            'created_table_count': created_table_count,
            'total_fields': total_fields,
            'total_records': total_records,
            'title': '系统首页 - Icent低代码平台',
        }

        return render(request, 'index.html', context)

    except Exception as e:
        logger.exception("首页加载失败")
        messages.error(request, "系统异常，首页数据加载失败，请联系管理员")
        # 异常时返回基础页面，避免白屏
        return render(request, 'index.html', {
            'model_configs': [],
            'current_time': timezone.now(),
            'total_models': 0,
            'created_table_count': 0,
            'total_fields': 0,
            'total_records': 0,
            'title': '系统首页 - Icent低代码平台',
        })


@staff_member_required
def model_upgrade_view(request: HttpRequest):
    """模型升级视图（管理员权限）"""
    lowcode_base_url = request.build_absolute_uri('/lowcode/')
    context = {
        'lowcode_base_url': lowcode_base_url,
        'title': '模型升级 - Icent低代码平台'
    }
    return render(request, 'lowcode/model_upgrade.html', context)


def dashboard_view(request: HttpRequest):
    """仪表盘视图（优化统计逻辑）"""
    static_names = {'DataPermission', 'LowCodeMethodCallLog', 'ModelLowCode', 'FieldModel', 'LowCodeUser', 'Role'}
    app_models = [m for m in apps.get_app_config('lowcode').get_models() if m.__name__ not in static_names]

    # 优化统计性能
    total_models = len(app_models)
    total_data = 0

    # 使用缓存批量统计
    for model in app_models:
        cache_key = f'lowcode_data_count_{model.__name__}'
        cnt = cache.get(cache_key)
        if cnt is None:
            try:
                cnt = model.objects.count()
                cache.set(cache_key, cnt, CACHE_TIMEOUT)
            except Exception as e:
                logger.warning(f"统计模型 '{model.__name__}' 记录数失败: {str(e)}")
                cnt = 0
        total_data += cnt

    overview_data: List[OverviewDataType] = [
        {
            'label': '动态模型总数',
            'value': total_models,
            'color': 'primary',
            'icon': 'bi-diagram-3',
            'url': reverse('lowcode:model-list'),  # 对应优化后的路由名称（kebab-case）
            'action_text': '前往管理'
        },
        {
            'label': '累计数据条数',
            'value': total_data,
            'color': 'success',
            'icon': 'bi-database',
            'url': reverse('lowcode:model-list'),  # 对应优化后的路由名称（kebab-case）
            'action_text': '查看详情'
        },
    ]

    return render(request, 'lowcode/dashboard.html', {
        'overview_data': overview_data,
        'title': 'Icent低代码平台 - 仪表盘'
    })


@staff_member_required
def model_list_view(request: HttpRequest):
    """所有动态模型列表视图（管理员权限，优化复用逻辑）"""
    try:
        model_configs = get_all_dynamic_model_configs()
        return render(request, 'lowcode/model_list.html', {
            'model_configs': model_configs,
            'title': '动态模型列表',
        })
    except Exception as e:
        logger.exception("动态模型列表加载失败")
        messages.error(request, "系统异常，模型列表加载失败，请联系管理员")
        return render(request, 'lowcode/model_list.html', {
            'model_configs': [],
            'title': '动态模型列表',
        })


# ========== Dynamic CRUD Views ==========
class DynamicModelListView(LoginRequiredMixin, ListView):
    """动态模型数据列表视图"""
    template_name = 'dynamic_model_list.html'
    context_object_name = 'object_list'
    paginate_by = 20

    def setup(self, request: HttpRequest, *args, **kwargs):
        super().setup(request, *args, **kwargs)
        self.model_name = self.kwargs['model_name']
        self.model_class = get_model_by_name(self.model_name)

    def get_queryset(self):
        """支持简单搜索功能"""
        queryset = self.model_class.objects.all()
        search_query = self.request.GET.get('search', '')
        if search_query:
            # 对所有字符型字段进行模糊搜索
            search_filters = Q()
            for field in self.model_class._meta.fields:
                if isinstance(field, (models.CharField, models.TextField, models.EmailField, models.URLField)):
                    search_filters |= Q(**{f'{field.name}__icontains': search_query})
            queryset = queryset.filter(search_filters)
        return queryset.order_by('-id')  # 默认按ID倒序

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
    """动态模型数据创建视图"""
    template_name = 'dynamic_model_form.html'

    def setup(self, request: HttpRequest, *args, **kwargs):
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
        # 对应优化后的路由名称（kebab-case）
        return reverse('lowcode:dynamic-model-list', kwargs={'model_name': self.model_name})


class DynamicModelDetailView(LoginRequiredMixin, DetailView):
    """动态模型数据详情视图"""
    template_name = 'dynamic_model_detail.html'

    def setup(self, request: HttpRequest, *args, **kwargs):
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

            # 处理特殊字段显示
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
    """动态模型数据更新视图"""
    template_name = 'dynamic_model_form.html'

    def setup(self, request: HttpRequest, *args, **kwargs):
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
        # 对应优化后的路由名称（kebab-case）
        return reverse('lowcode:dynamic-model-detail', kwargs={'model_name': self.model_name, 'pk': self.object.pk})


class DynamicModelDeleteView(LoginRequiredMixin, DeleteView):
    """动态模型数据删除视图"""
    template_name = 'dynamic_model_confirm_delete.html'

    def setup(self, request: HttpRequest, *args, **kwargs):
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
            'object_name': str(self.object)[:50],  # 显示对象名称（截取前50字符）
        })
        return context

    def get_success_url(self):
        messages.success(self.request, f'{self.model_class._meta.verbose_name} 已删除。')
        # 对应优化后的路由名称（kebab-case）
        return reverse('lowcode:dynamic-model-list', kwargs={'model_name': self.model_name})


# ========== API Views ==========
class StandardResultsSetPagination(PageNumberPagination):
    """标准分页配置"""
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class DynamicMethodCallLogViewSet(ReadOnlyModelViewSet):
    """方法调用日志API视图集"""
    serializer_class = LowCodeMethodCallLogSerializer
    permission_classes = [IsAdminUser]
    filter_backends = [DjangoFilterBackend, SearchFilter, OrderingFilter]
    filterset_fields = {
        "user": ["exact"],
        "model_name": ["exact", "icontains"],
        "method_name": ["exact", "icontains"],
        "result_status": ["exact"],
    }
    search_fields = ["model_name", "method_name", "exception_msg"]
    ordering_fields = ["call_time", "time_cost"]
    ordering = ["-call_time"]
    pagination_class = StandardResultsSetPagination

    def get_queryset(self):
        queryset = LowCodeMethodCallLog.objects.all()
        params = {
            "start_time": self.request.query_params.get("start_time"),
            "end_time": self.request.query_params.get("end_time")
        }
        return _apply_log_filters(queryset, params)


class BatchDataPermissionView(APIView):
    """批量数据权限授权API"""
    permission_classes = [IsAdminUser]

    def post(self, request):
        serializer = BatchDataPermissionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        # 去重处理，避免重复授权
        existing_perms = DataPermission.objects.filter(
            user_id__in=data["user_ids"],
            model_name=data["model_name"],
            data_id__in=data["data_ids"]
        ).values_list('user_id', 'data_id')
        existing_set = set(existing_perms)

        objs = []
        for u in data["user_ids"]:
            for d in data["data_ids"]:
                if (u, d) not in existing_set:
                    objs.append(DataPermission(user_id=u, model_name=data["model_name"], data_id=d))

        if objs:
            DataPermission.objects.bulk_create(objs, batch_size=100)

        return Response({
            "code": 200,
            "msg": f"成功授权{len(data['user_ids'])}个用户访问{len(data['data_ids'])}条{data['model_name']}数据（新增{len(objs)}条权限）",
            "data": {
                "user_ids": data["user_ids"],
                "data_ids": data["data_ids"],
                "model_name": data["model_name"],
                "total_count": len(objs),
                "duplicate_count": len(data["user_ids"]) * len(data["data_ids"]) - len(objs)
            }
        })


class MethodLogExportView(APIView):
    """方法调用日志导出API"""
    permission_classes = [IsAdminUser]

    def get(self, request):
        queryset = LowCodeMethodCallLog.objects.all()
        params = {k: request.query_params.get(k) for k in
                  ["user", "model_name", "method_name", "result_status", "start_time", "end_time"]}
        queryset = _apply_log_filters(queryset, params)

        record_count = queryset.count()
        if record_count > EXPORT_MAX_RECORDS:
            return Response({
                "code": 400,
                "msg": f"导出记录数超过最大限制 ({EXPORT_MAX_RECORDS})，当前{record_count}条",
                "data": {"max_records": EXPORT_MAX_RECORDS, "current_count": record_count}
            }, status=400)

        try:
            excel_buffer = generate_method_log_excel(queryset)
            response = HttpResponse(
                excel_buffer.getvalue(),
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            current_date = datetime.now().strftime("%Y%m%d_%H%M%S")
            response["Content-Disposition"] = f'attachment; filename="动态方法调用日志_{current_date}.xlsx"'
            excel_buffer.close()
            return response
        except Exception as e:
            logger.exception("导出方法调用日志失败")
            return Response({
                "code": 500,
                "msg": f"导出失败：{str(e)}"
            }, status=500)


class BatchRevokeDataPermissionView(APIView):
    """批量撤销数据权限API"""
    permission_classes = [IsAdminUser]

    def post(self, request):
        serializer = BatchRevokeDataPermissionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        q = Q()
        if data.get("user_ids"):
            q &= Q(user_id__in=data["user_ids"])
        if data.get("model_name"):
            q &= Q(model_name=data["model_name"])
        if data.get("data_ids"):
            q &= Q(data_id__in=data["data_ids"])

        if not q:
            return Response({
                "code": 400,
                "msg": "至少需要指定一个筛选条件（user_ids/model_name/data_ids）"
            }, status=400)

        deleted_count, _ = DataPermission.objects.filter(q).delete()
        return Response({
            "code": 200,
            "msg": f"成功撤销{deleted_count}条数据权限",
            "data": {
                "model_name": data.get("model_name"),
                "user_ids": data.get("user_ids", []),
                "data_ids": data.get("data_ids", []),
                "deleted_count": deleted_count
            }
        })


class AsyncExportMethodLogView(APIView):
    """异步导出方法调用日志API"""
    permission_classes = [IsAdminUser]

    def get(self, request):
        params = {k: v for k, v in {
            "user": request.query_params.get("user"),
            "model_name": request.query_params.get("model_name"),
            "result_status": request.query_params.get("result_status"),
            "start_time": request.query_params.get("start_time"),
            "end_time": request.query_params.get("end_time"),
        }.items() if v is not None}

        # 预检查记录数
        queryset = _apply_log_filters(LowCodeMethodCallLog.objects.all(), params)
        if queryset.count() > EXPORT_MAX_RECORDS * 5:  # 异步导出限制放宽5倍
            return Response({
                "code": 400,
                "msg": f"导出记录数超过最大限制 ({EXPORT_MAX_RECORDS * 5})",
                "data": {"max_records": EXPORT_MAX_RECORDS * 5, "current_count": queryset.count()}
            }, status=400)

        task = async_export_method_log.delay(params)
        return Response({
            "code": 200,
            "msg": "导出任务已启动，请查询进度",
            "data": {
                "task_id": task.id,
                "query_params": params,
                "estimated_count": queryset.count()
            }
        })


class ExportProgressView(APIView):
    """导出进度查询API"""
    permission_classes = [IsAdminUser]

    def get(self, request):
        task_id = request.query_params.get("task_id")
        if not task_id:
            return Response({"code": 400, "msg": "缺少task_id参数"}, status=400)

        progress = cache.get(f"export_progress_{task_id}")
        if progress is None:
            return Response({"code": 404, "msg": "任务不存在或已过期（有效期24小时）"}, status=404)

        data = {
            "task_id": task_id,
            "progress": progress,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        if progress == 100:
            file_path = cache.get(f"export_file_{task_id}")
            if file_path and default_storage.exists(file_path):
                data["download_url"] = request.build_absolute_uri(
                    f"/lowcode/api/v1/download-export/?file_path={file_path}")
                data["msg"] = "导出完成，可下载"
            else:
                data["msg"] = "导出完成，但文件不存在"
        elif progress == -1:
            error = cache.get(f"export_error_{task_id}", "未知错误")
            data["msg"] = f"导出失败：{error}"
        else:
            data["msg"] = f"导出中，进度{progress}%"

        return Response({"code": 200, "data": data})


class DownloadExportView(APIView):
    """下载导出文件API"""
    permission_classes = [IsAdminUser]

    def get(self, request):
        file_path = request.query_params.get("file_path")
        if not file_path:
            return Response({"code": 400, "msg": "缺少file_path参数"}, status=400)

        # 安全检查
        if not file_path.startswith(EXPORT_SUBDIR):
            return Response({"code": 400, "msg": "非法文件路径"}, status=400)

        try:
            filename = os.path.basename(file_path)
            _sanitize_filename(filename)

            if not default_storage.exists(file_path):
                return Response({"code": 404, "msg": "文件不存在或已过期"}, status=404)

            file = default_storage.open(file_path, 'rb')
            response = FileResponse(file,
                                    content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
            response["Content-Disposition"] = f'attachment; filename="{filename}"'
            return response
        except ValidationError as e:
            return Response({"code": 400, "msg": str(e)}, status=400)
        except Exception as e:
            logger.exception(f"下载文件失败：{file_path}")
            return Response({"code": 500, "msg": f"下载失败：{str(e)}"}, status=500)


# ========== Misc Views ==========
def prometheus_metrics(request: HttpRequest):
    """Prometheus监控指标视图"""
    try:
        return HttpResponse(generate_latest(), content_type=CONTENT_TYPE_LATEST)
    except Exception as e:
        logger.exception("获取监控指标失败")
        return HttpResponse(status=500)


@staff_member_required
def refresh_methods(request: HttpRequest):
    """刷新动态方法视图（限制管理员权限）"""
    try:
        refresh_dynamic_methods()
        # 清除相关缓存
        cache.delete_pattern('lowcode_data_count_*')
        cache.delete_pattern('lowcode_field_count_*')
        messages.success(request, "动态方法刷新成功")
        return JsonResponse({"code": 200, "msg": "动态方法刷新成功"})
    except Exception as e:
        logger.exception("刷新动态方法失败")
        messages.error(request, f"刷新失败：{str(e)}")
        return JsonResponse({"code": 500, "msg": f"刷新失败：{str(e)}"})


@staff_member_required
def call_dynamic_method(request: HttpRequest, model_name: str, instance_id: int, method_name: str):
    """调用动态模型方法视图（限制管理员权限）"""
    try:
        DynamicModel = get_dynamic_model_with_methods(model_name)
        if DynamicModel is None:
            raise Http404("模型不存在")

        instance = get_object_or_404(DynamicModel, id=instance_id)
        method = getattr(instance, method_name, None)

        if not method or not callable(method):
            raise Http404(f"方法 '{method_name}' 不存在或不可调用")

        # 记录方法调用日志
        logger.info(f"用户 {request.user.username} 调用模型 {model_name} 实例 {instance_id} 的方法 {method_name}")

        result = method(user=request.user)
        return JsonResponse({
            "code": 200,
            "msg": "方法调用成功",
            "data": {"result": result}
        })
    except Http404 as e:
        logger.warning(f"调用动态方法失败：{str(e)}")
        return JsonResponse({"code": 404, "msg": str(e)}, status=404)
    except Exception as e:
        logger.exception(f"调用动态方法异常：{model_name}.{method_name}")
        return JsonResponse({
            "code": 500,
            "msg": f"调用失败：{str(e)}",
            "error_detail": str(e) if settings.DEBUG else None
        }, status=500)


def create_lowcode_user_example(request: HttpRequest):
    """创建示例用户视图（仅用于演示）"""
    if not settings.DEBUG:
        return JsonResponse({"code": 403, "msg": "演示功能仅在开发环境可用"}, status=403)

    try:
        django_user, _ = User.objects.get_or_create(username="demo_user", defaults={"email": "demo@example.com"})
        role, _ = Role.objects.get_or_create(code="editor", defaults={"name": "编辑员"})
        lowcode_user, created = LowCodeUser.objects.update_or_create(
            user=django_user,
            defaults={
                "employee_id": "EMP1001",
                "department": "产品研发部",
                "phone": "13800138000",
                "role": role
            }
        )
        return JsonResponse({
            "code": 200,
            "status": "success" if created else "updated",
            "msg": "示例用户创建/更新成功",
            "data": {
                "username": lowcode_user.user.username,
                "email": lowcode_user.user.email,
                "employee_id": lowcode_user.employee_id,
                "department": lowcode_user.department,
                "phone": lowcode_user.phone,
                "role": lowcode_user.role.name if lowcode_user.role else None
            }
        })
    except Exception as e:
        logger.exception("创建示例用户失败")
        return JsonResponse({"code": 500, "msg": f"创建失败：{str(e)}"}, status=500)


@staff_member_required
def get_lowcode_user_detail(request: HttpRequest, user_id: int):
    """获取低代码用户详情视图（限制管理员权限）"""
    try:
        lowcode_user = get_object_or_404(LowCodeUser, user_id=user_id)
        return JsonResponse({
            "code": 200,
            "data": {
                "user_id": lowcode_user.user_id,
                "username": lowcode_user.user.username,
                "email": lowcode_user.user.email,
                "employee_id": lowcode_user.employee_id,
                "department": lowcode_user.department,
                "phone": lowcode_user.phone,
                "role": lowcode_user.role.name if lowcode_user.role else None,
                "avatar_url": lowcode_user.avatar.url if lowcode_user.avatar else None,
                "date_joined": lowcode_user.user.date_joined.strftime("%Y-%m-%d %H:%M:%S"),
                "last_login": lowcode_user.user.last_login.strftime(
                    "%Y-%m-%d %H:%M:%S") if lowcode_user.user.last_login else None
            }
        })
    except Exception as e:
        logger.exception(f"获取用户详情失败：user_id={user_id}")
        return JsonResponse({"code": 500, "msg": f"获取失败：{str(e)}"}, status=500)


# ========== API根路径视图（移至views.py便于复用） ==========
class APIRootView(APIView):
    """低代码平台API接口根文档"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response({
            "message": "低代码平台API接口文档（v1）",
            "version": "1.0.0",
            "description": "提供动态模型数据管理、权限控制、日志导出等功能",
            "endpoints": {
                "方法调用日志": request.build_absolute_uri("method-call-logs/"),
                "批量数据权限": request.build_absolute_uri("batch-data-permission/"),
                "日志导出": request.build_absolute_uri("export-method-logs/"),
                "异步日志导出": request.build_absolute_uri("async-export-method-logs/")
            },
            "notice": "生产环境建议限制API访问IP，避免敏感操作泄露"
        })