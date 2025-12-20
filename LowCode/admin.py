from __future__ import annotations

from typing import Optional, List, Tuple, Any
from django.utils.html import format_html
from django.utils import timezone
from django.db import connection, models
from django.db.models import QuerySet
from django.urls import reverse, NoReverseMatch
from django.apps import apps
import re
import logging

from django.contrib import admin
from django.contrib.auth.models import User
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

# 配置日志
logger = logging.getLogger(__name__)


# ========== 动态导入模型（解决IDE索引问题 + 避免循环引用） ==========
def get_model_safely(app_label: str, model_name: str) -> Optional[models.Model]:
    """安全获取Django模型，捕获异常并记录日志"""
    try:
        return apps.get_model(app_label, model_name)
    except LookupError as e:
        logger.warning(f"模型 {app_label}.{model_name} 未找到: {e}")
        return None
    except Exception as e:
        logger.error(f"获取模型 {app_label}.{model_name} 失败: {e}", exc_info=True)
        return None


# 懒加载模型
LowCodeModelConfig = get_model_safely("lowcode", "LowCodeModelConfig")
FieldModel = get_model_safely("lowcode", "FieldModel")
MethodLowCode = get_model_safely("lowcode", "MethodLowCode")
Document = get_model_safely("lowcode", "Document")
Role = get_model_safely("lowcode", "Role")
ModelUpgradeRecord = get_model_safely("lowcode", "ModelUpgradeRecord")
LowCodeMethodCallLog = get_model_safely("lowcode", "LowCodeMethodCallLog")
DataPermission = get_model_safely("lowcode", "DataPermission")
LowCodeUser = get_model_safely("lowcode", "LowCodeUser")


# ========== 通用工具函数 ==========
class AdminUtils:
    """Admin通用工具类"""

    @staticmethod
    def format_datetime(dt: Optional[timezone.datetime]) -> str:
        """统一格式化时间显示"""
        if not dt:
            return format_html('<div style="min-width: 180px; font-family: monospace;">-</div>')

        local_dt = timezone.localtime(dt)
        time_str = local_dt.strftime("%Y-%m-%d %H:%M:%S")
        return format_html(
            '<div style="min-width: 180px; font-family: monospace;">{}</div>',
            time_str
        )

    @staticmethod
    def get_model_admin_url(obj: models.Model, action: str = "change") -> str:
        """生成模型Admin操作URL，兼容反向解析失败"""
        if not obj or not hasattr(obj, "pk"):
            return "#"

        try:
            return reverse(f"admin:{obj._meta.app_label}_{obj._meta.model_name}_{action}", args=[obj.pk])
        except NoReverseMatch:
            return f"/admin/{obj._meta.app_label}/{obj._meta.model_name}/{obj.pk}/{action}/"

    @staticmethod
    def is_table_exists(table_name: str) -> bool:
        """跨数据库兼容的表存在性检查"""
        if not table_name:
            return False

        with connection.cursor() as cursor:
            vendor = connection.vendor
            try:
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
            except Exception as e:
                logger.error(f"检查表 {table_name} 存在性失败: {e}", exc_info=True)
                return False


# ========== 方法模型 Admin ==========
@admin.register(MethodLowCode) if MethodLowCode else None
class MethodLowCodeAdmin(admin.ModelAdmin):
    """动态方法配置Admin管理类"""
    list_display = ('method_name', 'model_name', 'logic_type', 'is_active', 'create_time')
    list_filter = ('logic_type', 'is_active', 'model_name')
    search_fields = ('method_name', 'model_name')
    filter_horizontal = ('roles',)
    readonly_fields = ('create_time', 'update_time')
    # 优化：添加分页
    list_per_page = 20

    def get_form(self, request, obj=None, **kwargs):
        """增强表单帮助文本，格式化HTML"""
        form = super().get_form(request, obj, **kwargs)

        # 优化：使用格式化字符串，提升可读性
        help_text = """
        <div style="padding: 10px; background-color: #f8f9fa; border-radius: 4px; margin: 5px 0;">
            <strong>参数格式约定：</strong><br>
            • <b>aggregate</b>: {"related_name": "...", "agg_field": "...", "operation": "sum"}<br>
            • <b>field_update</b>: {"field_name": "..."}<br>
            • <b>custom_func</b>: {"func_path": "myapp.utils.my_func"}<br>
            <small>支持的 operation: sum, avg, count, max, min</small>
        </div>
        """
        form.base_fields['params'].help_text = help_text
        return form


# ========== 内联字段管理 ==========
class FieldModelInline(admin.TabularInline):
    """动态模型字段内联编辑组件"""
    model = FieldModel
    extra = 1
    fields = ('name', 'label', 'type', 'required', 'options', 'help_text')
    ordering = ('name',)
    # 优化：添加字段样式
    classes = ('collapse',)
    verbose_name_plural = "字段配置"


# ========== 动态模型配置 Admin ==========
@admin.register(LowCodeModelConfig) if LowCodeModelConfig else None
class LowCodeModelConfigAdmin(admin.ModelAdmin):
    """动态模型配置Admin管理类"""
    list_display = [
        'id', 'model_name_link', 'table_name', 'fields_preview',
        'table_status_display', 'formatted_create_time',
        'formatted_update_time', 'go_to_model'
    ]
    search_fields = ['name', 'table_name']
    list_filter = ['create_time']
    readonly_fields = ['table_name', 'create_time', 'update_time']
    filter_horizontal = ('roles',)
    inlines = [FieldModelInline] if FieldModel else []
    list_per_page = 20
    # 优化：添加排序
    ordering = ('-create_time',)

    fieldsets = (
        ('基本信息', {
            'fields': ('name', 'table_name', 'roles'),
            'description': "<div style='color: #6c757d;'>数据表名将自动生成：lowcode_ + 模型名小写（不可手动修改）</div>"
        }),
        ('系统信息', {
            'fields': ('create_time', 'update_time'),
            'classes': ('collapse',)
        }),
    )

    # ========== 自定义列表显示字段 ==========
    def model_name_link(self, obj: LowCodeModelConfig) -> str:
        """生成模型名称链接，优化样式"""
        edit_url = AdminUtils.get_model_admin_url(obj)
        return format_html(
            '<a href="{}" style="color: #0d6efd; text-decoration: underline; font-weight: 500;">{}</a>',
            edit_url,
            obj.name or "未命名模型"
        )

    model_name_link.short_description = '模型名称'
    model_name_link.admin_order_field = 'name'

    def fields_preview(self, obj: LowCodeModelConfig) -> str:
        """优化字段预览，减少数据库查询"""
        # 优化：使用prefetch_related减少查询次数
        try:
            fields_qs: QuerySet = obj.fields.all() if hasattr(obj, 'fields') else FieldModel.objects.filter(
                model_name=obj.name)
            # 优化：仅查询需要的字段
            fields_qs = fields_qs.only('name', 'type', 'required', 'label')[:5]
        except Exception as e:
            logger.warning(f"获取模型 {obj.name} 字段失败: {e}")
            return format_html('<span style="color: #dc3545;">获取字段失败</span>')

        if not fields_qs.exists():
            return format_html('<span style="color: #6c757d;">无字段配置</span>')

        preview_items = []
        for f in fields_qs:
            name = f.name or '未知字段'
            ftype = f.get_type_display() if hasattr(f, 'get_type_display') else f.type
            required = "必填" if f.required else "可选"
            preview_items.append(f"{name}（{ftype}，{required}）")

        full_text = "、".join(preview_items)
        total = obj.fields.count() if hasattr(obj, 'fields') else FieldModel.objects.filter(model_name=obj.name).count()
        if total > 5:
            full_text += f" ... 共{total}个字段"

        return format_html(
            '<div style="min-width: 220px; white-space: normal; line-height: 1.4;">{}</div>',
            full_text
        )

    fields_preview.short_description = "字段配置预览"

    def table_status_display(self, obj: LowCodeModelConfig) -> str:
        """优化数据表状态显示样式"""
        exists = AdminUtils.is_table_exists(obj.table_name)
        color, text = ("#198754", "✅ 已创建") if exists else ("#dc3545", "❌ 未创建")
        return format_html(
            '<div style="min-width: 120px; text-align: center;">'
            '<span style="color: {}; font-weight: bold;">{}</span>'
            '</div>',
            color, text
        )

    table_status_display.short_description = "数据表状态"
    table_status_display.admin_order_field = 'table_name'

    def go_to_model(self, obj: LowCodeModelConfig) -> str:
        """优化模型跳转链接，增强校验"""
        if not obj.name:
            return format_html('<span style="color: #6c757d;">无效模型名</span>')

        # 优化：更严格的模型名过滤
        safe_name = re.sub(r'[^a-zA-Z0-9_-]', '', obj.name).lower()
        if not safe_name:
            return format_html('<span style="color: #6c757d;">无效模型名</span>')

        model_list_url = f'/lowcode/model/{safe_name}/'
        return format_html(
            '<div style="min-width: 100px; text-align: center;">'
            '<a href="{}" class="button btn-sm btn-primary" target="_blank">转向模型</a>'
            '</div>',
            model_list_url
        )

    go_to_model.short_description = "操作"

    def formatted_create_time(self, obj: LowCodeModelConfig) -> str:
        return AdminUtils.format_datetime(obj.create_time)

    formatted_create_time.short_description = "创建时间"
    formatted_create_time.admin_order_field = 'create_time'

    def formatted_update_time(self, obj: LowCodeModelConfig) -> str:
        return AdminUtils.format_datetime(obj.update_time)

    formatted_update_time.short_description = "更新时间"
    formatted_update_time.admin_order_field = 'update_time'


# ========== 字段模型单独注册 ==========
@admin.register(FieldModel) if FieldModel else None
class FieldModelAdmin(admin.ModelAdmin):
    """字段配置Admin管理类"""
    list_display = ('model_name', 'name', 'label', 'type', 'required')
    list_filter = ('type', 'required', 'model_name')
    search_fields = ('name', 'label', 'model_name')
    ordering = ('model_name', 'name')
    list_per_page = 20
    # 优化：添加字段筛选
    save_as = True
    save_on_top = True


# ========== 用户扩展 ==========
class LowCodeUserInline(admin.StackedInline):
    """低代码用户信息内联编辑"""
    model = LowCodeUser
    can_delete = False
    verbose_name = "低代码用户信息"
    verbose_name_plural = "低代码用户信息"
    fields = ('employee_id', 'department', 'phone', 'avatar', 'role')
    # 优化：添加只读字段控制
    readonly_fields = ('created_at',) if LowCodeUser and hasattr(LowCodeUser, 'created_at') else ()


class CustomUserAdmin(BaseUserAdmin):
    """自定义用户Admin，集成低代码用户信息"""
    inlines = (LowCodeUserInline,) if LowCodeUser else ()


# 安全注册用户模型
try:
    admin.site.unregister(User)
    admin.site.register(User, CustomUserAdmin)
except admin.sites.AlreadyRegistered:
    logger.warning("User模型已注册，跳过重新注册")
except Exception as e:
    logger.error(f"注册User模型失败: {e}", exc_info=True)


# ========== 单独注册 LowCodeUser ==========
@admin.register(LowCodeUser) if LowCodeUser else None
class LowCodeUserAdmin(admin.ModelAdmin):
    """低代码用户信息Admin管理类"""
    list_display = ('user', 'employee_id', 'department', 'phone', 'role', 'created_at')
    list_filter = ('department', 'role', 'created_at')
    search_fields = ('user__username', 'user__email', 'employee_id', 'department')
    autocomplete_fields = ['user', 'role'] if Role else ['user']
    list_per_page = 20

    # 优化：预加载关联数据，提升性能
    def get_queryset(self, request):
        return super().get_queryset(request).select_related('user', 'role')


# ========== 其他模型注册 ==========
@admin.register(Document) if Document else None
class DocumentAdmin(admin.ModelAdmin):
    list_display = ('title', 'uploaded_at', 'file_size', 'file')
    search_fields = ('title',)
    list_filter = ('uploaded_at',)
    list_per_page = 20
    readonly_fields = ('uploaded_at',)

    def file_size(self, obj):
        """显示文件大小"""
        if obj.file and hasattr(obj.file, 'size'):
            size = obj.file.size
            if size < 1024:
                return f"{size} B"
            elif size < 1024 * 1024:
                return f"{size / 1024:.1f} KB"
            else:
                return f"{size / (1024 * 1024):.1f} MB"
        return "-"

    file_size.short_description = "文件大小"


@admin.register(Role) if Role else None
class RoleAdmin(admin.ModelAdmin):
    list_display = ('name', 'code', 'create_time')
    search_fields = ('name', 'code')
    list_per_page = 20
    ordering = ('-create_time',)
    # 优化：添加唯一校验
    readonly_fields = ('create_time',)


@admin.register(ModelUpgradeRecord) if ModelUpgradeRecord else None
class ModelUpgradeRecordAdmin(admin.ModelAdmin):
    list_display = ('model_name', 'status', 'created_by', 'created_at', 'task_id')
    list_filter = ('status', 'created_at')
    readonly_fields = ('fields', 'error_message', 'created_at')
    list_per_page = 20
    # 优化：按创建时间倒序
    ordering = ('-created_at',)


@admin.register(LowCodeMethodCallLog) if LowCodeMethodCallLog else None
class LowCodeMethodCallLogAdmin(admin.ModelAdmin):
    list_display = ('model_name', 'method_name', 'user', 'result_status', 'call_time', 'time_cost')
    list_filter = ('result_status', 'model_name', 'call_time')
    readonly_fields = ('params', 'result_data', 'exception_msg')
    list_per_page = 50
    # 优化：按调用时间倒序
    ordering = ('-call_time',)
    # 优化：添加日期层级筛选
    date_hierarchy = 'call_time'


@admin.register(DataPermission) if DataPermission else None
class DataPermissionAdmin(admin.ModelAdmin):
    list_display = ('user', 'model_name', 'data_id', 'create_time')
    list_filter = ('model_name', 'create_time')
    search_fields = ('user__username', 'model_name', 'data_id')
    list_per_page = 20
    ordering = ('-create_time',)

    # 优化：预加载用户数据
    def get_queryset(self, request):
        return super().get_queryset(request).select_related('user')