# lowcode/admin.py
from django.utils.html import format_html
from django.utils import timezone
from django.db import connection
from django.urls import reverse, NoReverseMatch
import re

from django.contrib import admin
from django.contrib.auth.models import User
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

# Models
from .models.models import (
    ModelLowCode, FieldModel, MethodLowCode,
    Document, Role, ModelUpgradeRecord,
    LowCodeMethodCallLog, DataPermission, LowCodeUser
)


# ========== 方法模型 Admin ==========
@admin.register(MethodLowCode)
class MethodLowCodeAdmin(admin.ModelAdmin):
    list_display = ('method_name', 'model_name', 'logic_type', 'is_active', 'create_time')
    list_filter = ('logic_type', 'is_active', 'model_name')
    search_fields = ('method_name', 'model_name')
    filter_horizontal = ('roles',)
    readonly_fields = ('create_time', 'update_time')

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        help_text = """
        <strong>参数格式约定：</strong><br>
        • <b>aggregate</b>: {"related_name": "...", "agg_field": "...", "operation": "sum"}<br>
        • <b>field_update</b>: {"field_name": "..."}<br>
        • <b>custom_func</b>: {"func_path": "myapp.utils.my_func"}<br>
        支持的 operation: sum, avg, count, max, min
        """
        form.base_fields['params'].help_text = help_text
        return form


# ========== 内联字段管理 ==========
class FieldModelInline(admin.TabularInline):
    model = FieldModel
    extra = 1
    fields = ('name', 'label', 'type', 'required', 'options', 'help_text', 'order')
    ordering = ('order',)


# ========== 动态模型配置 Admin ==========
@admin.register(ModelLowCode)
class ModelLowCodeAdmin(admin.ModelAdmin):
    list_display = [
        'id',
        'model_name_link',
        'table_name',
        'fields_preview',
        'table_status_display',
        'formatted_create_time',
        'formatted_update_time',
        'go_to_model',
    ]
    search_fields = ['name', 'table_name']
    list_filter = ['create_time']
    readonly_fields = ['table_name', 'create_time', 'update_time']
    filter_horizontal = ('roles',)
    inlines = [FieldModelInline]

    fieldsets = (
        ('基本信息', {
            'fields': ('name', 'table_name', 'roles'),
            'description': "数据表名将自动生成：lowcode_ + 模型名小写（不可手动修改）"
        }),
        ('系统信息', {
            'fields': ('create_time', 'update_time'),
            'classes': ('collapse',)
        }),
    )

    def model_name_link(self, obj):
        try:
            edit_url = reverse('admin:lowcode_modellowcode_change', args=[obj.pk])
        except NoReverseMatch:
            edit_url = f'/admin/lowcode/modellowcode/{obj.id}/change/'
        return format_html(
            '<a href="{}" style="color: #007bff; text-decoration: underline; font-weight: 500;">{}</a>',
            edit_url,
            obj.name
        )
    model_name_link.short_description = '模型名称'
    model_name_link.admin_order_field = 'name'

    def fields_preview(self, obj):
        fields_qs = obj.fields.all()
        if not fields_qs.exists():
            return "无字段配置"

        preview_items = []
        for f in fields_qs[:5]:
            name = f.name or '未知字段'
            ftype = f.get_type_display() if hasattr(f, 'get_type_display') else f.type
            required = "必填" if f.required else "可选"
            preview_items.append(f"{name}（{ftype}，{required}）")

        full_text = "、".join(preview_items)
        total = fields_qs.count()
        if total > 5:
            full_text += f" ... 共{total}个字段"

        return format_html(
            '<div style="min-width: 220px; white-space: normal; line-height: 1.4;">{}</div>',
            full_text
        )
    fields_preview.short_description = "字段配置预览"

    def table_status_display(self, obj):
        exists = self._is_table_exists(obj.table_name)
        color, text = ("#28a745", "✅ 已创建") if exists else ("#dc3545", "❌ 未创建")
        return format_html(
            '<div style="min-width: 120px; text-align: center;">'
            '<span style="color: {}; font-weight: bold;">{}</span>'
            '</div>',
            color, text
        )
    table_status_display.short_description = "数据表状态"
    table_status_display.admin_order_field = 'table_name'

    def go_to_model(self, obj):
        safe_name = re.sub(r'[^a-zA-Z0-9_-]', '', obj.name).lower()
        if not safe_name:
            return format_html('<span style="color: #6c757d;">无效模型名</span>')
        model_list_url = f'/lowcode/model/{safe_name}/'
        return format_html(
            '<div style="min-width: 100px; text-align: center;">'
            '<a href="{}" class="button" target="_blank">转向模型</a>'
            '</div>',
            model_list_url
        )
    go_to_model.short_description = "操作"

    def formatted_create_time(self, obj):
        return self._format_datetime(obj.create_time)
    formatted_create_time.short_description = "创建时间"
    formatted_create_time.admin_order_field = 'create_time'

    def formatted_update_time(self, obj):
        return self._format_datetime(obj.update_time)
    formatted_update_time.short_description = "更新时间"
    formatted_update_time.admin_order_field = 'update_time'

    def _format_datetime(self, dt):
        if dt:
            local_dt = timezone.localtime(dt)
            time_str = local_dt.strftime("%Y-%m-%d %H:%M:%S")
        else:
            time_str = "-"
        return format_html('<div style="min-width: 180px; font-family: monospace;">{}</div>', time_str)

    def _is_table_exists(self, table_name):
        if not table_name:
            return False
        with connection.cursor() as cursor:
            vendor = connection.vendor
            if vendor == 'postgresql':
                cursor.execute("SELECT to_regclass(%s)", [table_name])
                return cursor.fetchone()[0] is not None
            elif vendor in ('mysql', 'mariadb'):
                db_name = connection.settings_dict['NAME']
                cursor.execute(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_schema = %s AND table_name = %s",
                    [db_name, table_name]
                )
                return cursor.fetchone() is not None
            else:  # SQLite and others
                cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=%s;", [table_name])
                return cursor.fetchone() is not None
        return False


# ========== 字段模型单独注册 ==========
@admin.register(FieldModel)
class FieldModelAdmin(admin.ModelAdmin):
    list_display = ('model_config', 'name', 'label', 'type', 'required', 'order')
    list_filter = ('model_config__name', 'type', 'required')
    search_fields = ('name', 'label', 'model_config__name')
    ordering = ('model_config', 'order')


# ========== 用户扩展 ==========
class LowCodeUserInline(admin.StackedInline):
    model = LowCodeUser
    can_delete = False
    verbose_name = "低代码用户信息"
    verbose_name_plural = "低代码用户信息"
    fields = ('employee_id', 'department', 'phone', 'avatar', 'role')


class UserAdmin(BaseUserAdmin):
    inlines = (LowCodeUserInline,)


# 重新注册 User
admin.site.unregister(User)
admin.site.register(User, UserAdmin)


# ========== 单独注册 LowCodeUser ==========
@admin.register(LowCodeUser)
class LowCodeUserAdmin(admin.ModelAdmin):
    list_display = ('user', 'employee_id', 'department', 'phone', 'role', 'created_at')
    list_filter = ('department', 'role', 'created_at')
    search_fields = ('user__username', 'user__email', 'employee_id', 'department')
    autocomplete_fields = ['user', 'role']


# ========== 其他模型注册 ==========
@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = ('title', 'uploaded_at', 'file')
    search_fields = ('title',)


@admin.register(Role)
class RoleAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'create_time')
    search_fields = ('name', 'code')


@admin.register(ModelUpgradeRecord)
class ModelUpgradeRecordAdmin(admin.ModelAdmin):
    list_display = ('model_name', 'status', 'created_by', 'created_at', 'task_id')
    list_filter = ('status', 'created_at')
    readonly_fields = ('fields', 'error_message')


@admin.register(LowCodeMethodCallLog)
class LowCodeMethodCallLogAdmin(admin.ModelAdmin):
    list_display = ('model_name', 'method_name', 'user', 'result_status', 'call_time', 'time_cost')
    list_filter = ('result_status', 'model_name', 'call_time')
    readonly_fields = ('params', 'result_data', 'exception_msg')


@admin.register(DataPermission)
class DataPermissionAdmin(admin.ModelAdmin):
    list_display = ('user', 'model_name', 'data_id', 'create_time')
    list_filter = ('model_name', 'create_time')
    search_fields = ('user__username', 'model_name', 'data_id')