# lowcode/models/models.py
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Dict, List, TypedDict

from django.core.exceptions import ValidationError
from django.db import models, IntegrityError
from django.db.models import Q
from django.contrib.auth.models import User

if TYPE_CHECKING:
    from django.db.models.manager import Manager


# ========== 类型定义 ==========
class FieldConfig(TypedDict):
    name: str
    type: str
    required: bool
    options: List[str]
    label: str
    help_text: str


# ========== 字段类型常量 ==========
FIELD_TYPES = [
    ("char", "单行文本"),
    ("text", "多行文本"),
    ("integer", "整数"),
    ("float", "小数"),
    ("boolean", "布尔值"),
    ("date", "日期"),
    ("datetime", "日期时间"),
    ("email", "邮箱"),
    ("url", "网址"),
    ("choice", "下拉选项"),
    ("foreignkey", "关联其他模型"),
]

ALLOWED_FIELD_TYPE_VALUES = {t[0] for t in FIELD_TYPES}


# ========== 工具函数 ==========
def validate_python_identifier(value: str) -> None:
    """校验是否为合法的 Python 标识符（用于 model_name / field name）"""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', value):
        raise ValidationError(f"'{value}' 不是有效的 Python 标识符")


def validate_table_name(value: str) -> None:
    """校验表名合法性（仅允许字母、数字、下划线，且不以数字开头）"""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', value):
        raise ValidationError("表名必须是有效的数据库标识符（字母、数字、下划线，不以数字开头）")


# ========== 静态平台模型 ==========
class Document(models.Model):
    """用户上传的文档文件"""
    title = models.CharField(max_length=100, verbose_name="标题")
    file = models.FileField(
        upload_to='documents/',
        verbose_name="文件",
        help_text="上传的文档将存储在 media/documents/ 目录下"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True, verbose_name="上传时间")

    class Meta:
        db_table = "lowcode_document"
        verbose_name = "文档"
        verbose_name_plural = "文档"

    def __str__(self):
        return self.title


class Role(models.Model):
    """系统角色，用于权限分组"""
    name = models.CharField(max_length=32, unique=True, verbose_name="角色名")
    code = models.CharField(max_length=32, unique=True, verbose_name="角色编码", help_text="用于程序识别，如 admin, editor")
    create_time = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        db_table = "lowcode_role"
        verbose_name = "角色"
        verbose_name_plural = "角色"

    def __str__(self):
        return f"{self.name} ({self.code})"


class ModelUpgradeRecord(models.Model):
    """动态模型升级任务记录（支持异步迁移）"""
    STATUS_PENDING = 'pending'
    STATUS_RUNNING = 'running'
    STATUS_SUCCESS = 'success'
    STATUS_FAILED = 'failed'

    STATUS_CHOICES = [
        (STATUS_PENDING, '待处理'),
        (STATUS_RUNNING, '进行中'),
        (STATUS_SUCCESS, '成功'),
        (STATUS_FAILED, '失败'),
    ]

    model_name = models.CharField(
        max_length=64,
        verbose_name="模型名称",
        help_text="动态模型类名",
        validators=[validate_python_identifier],
        db_index=True
    )
    fields = models.JSONField(verbose_name="字段定义快照")
    status = models.CharField(
        max_length=20,
        choices=STATUS_CHOICES,
        default=STATUS_PENDING,
        verbose_name="状态",
        db_index=True
    )
    error_message = models.TextField(blank=True, verbose_name="错误信息")
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="创建人"
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    task_id = models.CharField(max_length=100, unique=True, verbose_name="任务ID", help_text="Celery 或其他任务队列的 ID")

    class Meta:
        db_table = "lowcode_model_upgrade_record"
        ordering = ['-created_at']
        verbose_name = "模型升级记录"
        verbose_name_plural = "模型升级记录"

    def __str__(self):
        return f"{self.model_name} - {self.get_status_display()} ({self.task_id})"


class LowCodeMethodCallLog(models.Model):
    """动态方法调用日志，用于审计与调试"""
    RESULT_SUCCESS = 'success'
    RESULT_FAIL = 'fail'

    RESULT_CHOICES = [
        (RESULT_SUCCESS, '成功'),
        (RESULT_FAIL, '失败'),
    ]

    user = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="lowcode_method_call_logs",
        verbose_name="调用用户"
    )
    model_name = models.CharField(max_length=64, verbose_name="动态模型类名", help_text="如 Product, Order", db_index=True)
    method_name = models.CharField(max_length=64, verbose_name="方法名", db_index=True)
    params = models.JSONField(null=True, blank=True, verbose_name="调用参数")
    result_status = models.CharField(
        max_length=16,
        choices=RESULT_CHOICES,
        verbose_name="结果状态",
        db_index=True
    )
    result_data = models.JSONField(null=True, blank=True, verbose_name="返回数据")
    exception_msg = models.TextField(null=True, blank=True, verbose_name="异常堆栈或消息")
    call_time = models.DateTimeField(auto_now_add=True, verbose_name="调用时间")
    time_cost = models.DecimalField(
        max_digits=10,
        decimal_places=6,
        null=True,
        blank=True,
        verbose_name="耗时（秒）",
        help_text="单位：秒，精确到微秒"
    )

    class Meta:
        db_table = "lowcode_method_call_log"
        verbose_name = "动态方法调用日志"
        verbose_name_plural = "动态方法调用日志"
        indexes = [
            models.Index(fields=["user", "call_time"]),
            models.Index(fields=["model_name", "method_name", "call_time"]),
            models.Index(fields=["result_status", "call_time"]),
        ]

    def __str__(self):
        return f"{self.model_name}.{self.method_name} @ {self.call_time}"


class DataPermission(models.Model):
    """数据级权限控制：用户对特定动态模型实例的访问授权"""
    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="lowcode_data_permissions",
        verbose_name="用户"
    )
    model_name = models.CharField(
        max_length=64,
        verbose_name="模型类名",
        help_text="动态模型名称，如 'Order'",
        validators=[validate_python_identifier],
        db_index=True
    )
    data_id = models.CharField(max_length=64, verbose_name="数据ID", help_text="动态模型实例的主键值（字符串形式）", db_index=True)
    create_time = models.DateTimeField(auto_now_add=True, verbose_name="授权时间")

    class Meta:
        db_table = "lowcode_data_permission"
        verbose_name = "数据权限"
        verbose_name_plural = "数据权限"
        unique_together = ("user", "model_name", "data_id")
        indexes = [
            models.Index(fields=["user", "model_name"]),
            models.Index(fields=["model_name", "data_id"]),
        ]

    def clean(self):
        validate_python_identifier(self.model_name)

    def __str__(self):
        return f"{self.user.username} → {self.model_name}[{self.data_id}]"


class LowCodeUser(models.Model):
    """低代码平台扩展用户信息"""
    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="lowcode_profile",
        verbose_name="关联用户"
    )
    employee_id = models.CharField(max_length=20, blank=True, null=True, verbose_name="员工编号")
    department = models.CharField(max_length=100, blank=True, verbose_name="部门")
    phone = models.CharField(max_length=20, blank=True, verbose_name="联系电话")
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True, verbose_name="头像")
    role = models.ForeignKey(
        Role,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="users",
        verbose_name="系统角色"
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")

    class Meta:
        db_table = "lowcode_user"
        verbose_name = "低代码用户"
        verbose_name_plural = "低代码用户"
        constraints = [
            models.UniqueConstraint(
                fields=['employee_id'],
                condition=Q(employee_id__isnull=False),
                name='unique_employee_id_if_not_null'
            )
        ]

    def __str__(self):
        return f"{self.user.username} ({self.employee_id or '无工号'})"


# ========== 动态模型元数据 ==========
class FieldModel(models.Model):
    model_config = models.ForeignKey(
        'ModelLowCode',
        on_delete=models.CASCADE,
        related_name='fields',
        verbose_name="所属动态模型"
    )
    name = models.CharField("字段名", max_length=100, validators=[validate_python_identifier])
    label = models.CharField("标签", max_length=100, blank=True)
    type = models.CharField("字段类型", max_length=20, choices=FIELD_TYPES)
    required = models.BooleanField("必填", default=False)
    # 注意：options 以换行符分隔，每行为一个选项值（如 "A\nB\nC"）
    options = models.TextField("选项（每行一个，用于下拉框等）", blank=True, help_text="仅用于 choice 类型，每行一个选项值")
    order = models.PositiveIntegerField("排序", default=0, help_text="字段在表单中的显示顺序")
    help_text = models.CharField("帮助文本", max_length=200, blank=True)

    class Meta:
        verbose_name = "动态字段"
        verbose_name_plural = "动态字段列表"
        unique_together = ('model_config', 'name')
        ordering = ['order', 'id']

    def clean(self):
        super().clean()
        if self.type not in ALLOWED_FIELD_TYPE_VALUES:
            raise ValidationError(f"无效的字段类型: {self.type}")

    def __str__(self) -> str:
        return f"{self.label or self.name} ({self.get_type_display()})"


class ModelLowCode(models.Model):
    if TYPE_CHECKING:
        fields: Manager["FieldModel"]

    name = models.CharField(
        max_length=50,
        verbose_name='模型名称',
        unique=True,
        db_index=True,
        validators=[validate_python_identifier]
    )
    table_name = models.CharField(
        max_length=100,
        verbose_name='数据表名',
        unique=True,
        db_index=True,
        validators=[validate_table_name]
    )
    roles = models.ManyToManyField(Role, related_name="dynamic_models", verbose_name="可访问角色")
    create_time = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    update_time = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        verbose_name = '动态模型配置'
        verbose_name_plural = '动态模型配置列表'
        db_table = 'lowcode_model_config'
        ordering = ['-create_time']

    def __str__(self) -> str:
        return f"{self.name}（表名：{self.table_name}）"

    def _generate_candidate_name(self, attempt: int) -> str:
        base = f'lowcode_{self.name.lower()}'
        if attempt == 0:
            return base
        return f'{base}_{attempt}'

    def save(self, *args, **kwargs) -> None:
        if not self.table_name:
            last_error = None
            for attempt in range(10):
                candidate = self._generate_candidate_name(attempt)
                self.table_name = candidate
                try:
                    validate_table_name(self.table_name)
                    super().save(*args, **kwargs)
                    return
                except IntegrityError as e:
                    last_error = e
                    continue
            raise ValidationError("无法生成唯一表名，请更换模型名称") from last_error
        else:
            validate_table_name(self.table_name)
            super().save(*args, **kwargs)

    def get_field_configs(self) -> List[FieldConfig]:
        # 注意：调用方应确保已 prefetch_related('fields')
        return [
            {
                'name': f.name,
                'type': f.type,
                'required': f.required,
                'options': f.options.splitlines() if f.options else [],
                'label': f.label,
                'help_text': f.help_text,
            }
            for f in self.fields.all()
        ]


class MethodLowCode(models.Model):
    AGGREGATE_PARAMS_SCHEMA = {
        "required": ["related_name", "agg_field"],
        "optional": ["operation", "multiply_field"],
        "defaults": {"operation": "sum"}
    }

    FIELD_UPDATE_PARAMS_SCHEMA = {
        "required": ["field_name"]
    }

    CUSTOM_FUNC_PARAMS_SCHEMA = {
        "required": ["func_path"]
    }

    LOGIC_TYPE_SCHEMAS = {
        "aggregate": AGGREGATE_PARAMS_SCHEMA,
        "field_update": FIELD_UPDATE_PARAMS_SCHEMA,
        "custom_func": CUSTOM_FUNC_PARAMS_SCHEMA,
    }

    # ⚠️ 安全建议：生产环境应限制 func_path 白名单
    CUSTOM_FUNC_WHITELIST_PREFIXES = [
        "lowcode.methods.",
        "myproject.custom_funcs.",
    ]

    method_name = models.CharField(max_length=64, verbose_name="自定义方法名", db_index=True, validators=[validate_python_identifier])
    model_name = models.CharField(max_length=64, verbose_name="动态模型类名", db_index=True, validators=[validate_python_identifier])
    logic_type = models.CharField(max_length=32, verbose_name="逻辑模板类型", choices=[
        ("aggregate", "聚合计算"),
        ("field_update", "字段更新"),
        ("custom_func", "自定义函数")
    ])
    params = models.JSONField(verbose_name="方法参数配置")
    # 可选冗余字段：便于高效查询 custom_func 的路径（仅当 logic_type='custom_func' 时有效）
    custom_func_path = models.CharField(
        max_length=255,
        blank=True,
        db_index=True,
        verbose_name="自定义函数路径（冗余）",
        help_text="仅用于 custom_func 类型，加速查询"
    )
    is_active = models.BooleanField(default=True, verbose_name="是否启用")
    roles = models.ManyToManyField(Role, related_name="dynamic_methods", verbose_name="可访问角色")
    create_time = models.DateTimeField(auto_now_add=True, verbose_name="创建时间")
    update_time = models.DateTimeField(auto_now=True, verbose_name="更新时间")

    class Meta:
        db_table = "lowcode_method_config"
        verbose_name = "动态方法配置"
        verbose_name_plural = "动态方法配置"
        unique_together = ("model_name", "method_name")

    def __str__(self) -> str:
        return f"{self.model_name}.{self.method_name} ({self.logic_type})"

    def clean(self):
        super().clean()
        if not self.logic_type:
            raise ValidationError("必须指定 logic_type")

        schema = self.LOGIC_TYPE_SCHEMAS.get(self.logic_type)
        if not schema:
            raise ValidationError(f"不支持的 logic_type: {self.logic_type}")

        params = self.params or {}
        if not isinstance(params, dict):
            raise ValidationError("params 必须是 JSON 对象")

        missing = [k for k in schema["required"] if k not in params]
        if missing:
            raise ValidationError(
                f"logic_type='{self.logic_type}' 要求 params 包含字段: {missing}"
            )

        if self.logic_type == "aggregate":
            op = str(params.get("operation", "sum")).lower()
            allowed_ops = {"sum", "avg", "count", "max", "min"}
            if op not in allowed_ops:
                raise ValidationError(
                    f"聚合操作 '{op}' 不受支持，允许值: {sorted(allowed_ops)}"
                )

        if self.logic_type == "custom_func":
            func_path = params.get("func_path", "")
            if not func_path or "." not in func_path:
                raise ValidationError("func_path 应为 'module.submodule.func_name' 格式")
            # 安全校验：限制模块前缀
            if not any(func_path.startswith(prefix) for prefix in self.CUSTOM_FUNC_WHITELIST_PREFIXES):
                raise ValidationError(
                    f"func_path 必须以白名单前缀开头: {self.CUSTOM_FUNC_WHITELIST_PREFIXES}"
                )
            self.custom_func_path = func_path
        else:
            self.custom_func_path = ""

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)