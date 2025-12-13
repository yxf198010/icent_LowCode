# lowcode/models/models.py
# # 1. 创建动态模型（自动同步到注册表+创建表）
# model = ModelLowCode.objects.create(
#     name="Product",
#     description="产品模型",
#     is_active=True
# )
# # 添加字段
# FieldModel.objects.create(
#     model_config=model,
#     name="name",
#     label="产品名称",
#     type="char",
#     required=True,
#     options_text="max_length:100"
# )
# FieldModel.objects.create(
#     model_config=model,
#     name="price",
#     label="产品价格",
#     type="decimal",
#     required=True,
#     options_text="10:2"
# )
#
# # 2. 手动同步模型到注册表
# model.sync_to_dynamic_registry(create_table=True)
#
# # 3. 获取动态模型类
# from lowcode.dynamic_model_registry import get_dynamic_model
# Product = get_dynamic_model("Product")
#
# # 4. 使用动态模型操作数据
# Product.objects.create(name="手机", price=1999.99)
from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Dict, List, TypedDict

from django.core.exceptions import ValidationError
from django.db import models, IntegrityError
from django.db.models import Q
from django.contrib.auth.models import User
from django.conf import settings
from pathlib import Path
import uuid  # ✅ 新增：导入uuid模块

if TYPE_CHECKING:
    from django.db.models.manager import Manager

# 导入动态模型注册中心核心方法（用于模型同步）
from lowcode.dynamic_model_registry import (
    _create_dynamic_model,
    _DYNAMIC_MODEL_REGISTRY,
    create_dynamic_model_table,
    SUPPORTED_FIELD_TYPES as DYNAMIC_SUPPORTED_FIELDS
)


# ========== 类型定义 ==========
class FieldConfig(TypedDict):
    name: str
    type: str
    required: bool
    options: List[str]
    label: str
    help_text: str
    options_dict: Dict[str, Any]  # 新增：适配动态模型的options参数


# ========== 字段类型常量（对齐动态模型工厂） ==========
# 映射关系：前端/Admin显示名 → Django字段类型名 → 中文描述
FIELD_TYPE_MAPPING = {
    "char": ("CharField", "单行文本"),
    "text": ("TextField", "多行文本"),
    "integer": ("IntegerField", "整数"),
    "big_integer": ("BigIntegerField", "大整数"),
    "small_integer": ("SmallIntegerField", "小整数"),
    "positive_integer": ("PositiveIntegerField", "正整数"),
    "positive_small_integer": ("PositiveSmallIntegerField", "正小整数"),
    "boolean": ("BooleanField", "布尔值"),
    "date": ("DateField", "日期"),
    "datetime": ("DateTimeField", "日期时间"),
    "time": ("TimeField", "时间"),
    "email": ("EmailField", "邮箱"),
    "url": ("URLField", "网址"),
    "decimal": ("DecimalField", "小数"),
    "float": ("FloatField", "浮点数"),
    "uuid": ("UUIDField", "UUID"),
    "json": ("JSONField", "JSON"),
    "file": ("FileField", "文件"),
    "image": ("ImageField", "图片"),
    "choice": ("CharField", "下拉选项"),  # 基于CharField实现
    "foreignkey": ("ForeignKey", "关联其他模型"),
}

# 用于Admin显示的字段类型选项
FIELD_TYPES = [(k, v[1]) for k, v in FIELD_TYPE_MAPPING.items()]
ALLOWED_FIELD_TYPE_VALUES = {k for k in FIELD_TYPE_MAPPING.keys()}

# 字段默认参数（对齐动态模型工厂的FIELD_DEFAULT_OPTIONS）
FIELD_DEFAULT_OPTIONS = {
    "char": {"max_length": 255},
    "decimal": {"max_digits": 10, "decimal_places": 2},
    "file": {"upload_to": "lowcode/files/"},
    "image": {"upload_to": "lowcode/images/"},
    # ✅ 修复：替换 models.UUIDField.default 为 uuid.uuid4
    "uuid": {"default": uuid.uuid4},
    "choice": {"max_length": 255},
}


# ========== 工具函数 ==========
def validate_python_identifier(value: str) -> None:
    """校验是否为合法的 Python 标识符（用于 model_name / field name）"""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', value):
        raise ValidationError(
            f"'{value}' 不是有效的 Python 标识符（字母/下划线开头，仅含字母/数字/下划线）"
        )


def validate_table_name(value: str) -> None:
    """校验表名合法性（仅允许字母、数字、下划线，且不以数字开头）"""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', value):
        raise ValidationError("表名必须是有效的数据库标识符（字母、数字、下划线，不以数字开头）")


def get_django_field_type(field_key: str) -> str:
    """将前端字段类型转换为Django字段类型名"""
    return FIELD_TYPE_MAPPING.get(field_key, ("CharField", "单行文本"))[0]


def parse_field_options(field_model: FieldModel) -> Dict[str, Any]:
    """将FieldModel转换为动态模型的字段配置参数"""
    field_key = field_model.type
    options = FIELD_DEFAULT_OPTIONS.get(field_key, {}).copy()

    # 基础通用参数
    options["verbose_name"] = field_model.label or field_model.name
    options["help_text"] = field_model.help_text
    options["null"] = not field_model.required
    options["blank"] = not field_model.required

    # 特殊字段处理
    if field_key == "choice":
        # 解析下拉选项（格式：值1:标签1;值2:标签2 或 每行一个）
        choices = []
        if field_model.options:
            # 兼容两种格式：分号分隔 或 换行分隔
            raw_options = field_model.options.replace('\n', ';').split(';')
            for opt in raw_options:
                opt = opt.strip()
                if not opt:
                    continue
                if ':' in opt:
                    val, label = opt.split(':', 1)
                    choices.append((val.strip(), label.strip()))
                else:
                    choices.append((opt, opt))
        options["choices"] = choices if choices else [("", "请选择")]

    elif field_key == "foreignkey":
        # 解析外键目标模型（格式：app.model 或 模型名）
        if field_model.options:
            to_model = field_model.options.strip()
            options["to"] = to_model if '.' in to_model else f"lowcode.{to_model}"
            options["on_delete"] = models.CASCADE  # 默认级联删除

    elif field_key == "decimal":
        # 解析小数配置（格式：max_digits:decimal_places，如 10:2）
        if field_model.options:
            try:
                max_digits, decimal_places = field_model.options.split(':', 1)
                options["max_digits"] = int(max_digits.strip())
                options["decimal_places"] = int(decimal_places.strip())
            except (ValueError, IndexError):
                pass  # 使用默认值

    return options


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
        validators=[validate_table_name],
        blank=True,  # 改为可空，由save方法自动生成
        null=False
    )
    description = models.TextField("模型描述", blank=True, help_text="可选，用于说明模型用途")
    roles = models.ManyToManyField(Role, related_name="dynamic_models", verbose_name="可访问角色")
    is_active = models.BooleanField("是否启用", default=True, help_text="禁用后将无法访问该模型")
    create_time = models.DateTimeField(auto_now_add=True, verbose_name='创建时间')
    update_time = models.DateTimeField(auto_now=True, verbose_name='更新时间')

    class Meta:
        verbose_name = '动态模型配置'
        verbose_name_plural = '动态模型配置列表'
        db_table = 'lowcode_model_config'
        ordering = ['-create_time']
        indexes = [
            models.Index(fields=["is_active", "name"]),
        ]

    def __str__(self) -> str:
        status = "✅" if self.is_active else "❌"
        return f"{status} {self.name}（表名：{self.table_name}）"

    def _generate_candidate_name(self, attempt: int) -> str:
        """生成表名（对齐动态模型工厂的表名规则：lowcode_模型名小写）"""
        base = f'lowcode_{self.name.lower()}'
        if attempt == 0:
            return base
        return f'{base}_{attempt}'

    def get_dynamic_field_config(self) -> Dict[str, Dict[str, Any]]:
        """转换为动态模型工厂需要的字段配置格式"""
        field_config = {}
        for field in self.fields.all():
            django_field_type = get_django_field_type(field.type)
            field_config[field.name] = {
                "type": django_field_type,
                "options": field.get_final_options()
            }
        return field_config

    def sync_to_dynamic_registry(self, create_table: bool = True) -> bool:
        """同步到动态模型注册表，并可选创建数据表"""
        try:
            # 1. 生成动态模型配置
            field_config = self.get_dynamic_field_config()
            # 2. 创建动态模型类并注册
            model_class = _create_dynamic_model(self.name, field_config)
            _DYNAMIC_MODEL_REGISTRY[self.name] = model_class
            # 3. 可选创建数据表
            if create_table and self.is_active:
                create_dynamic_model_table(self.name)
            return True
        except Exception as e:
            raise ValidationError(f"同步动态模型失败：{str(e)}") from e

    def save(self, *args, **kwargs) -> None:
        # 自动生成表名（对齐动态模型工厂规则）
        if not self.table_name:
            last_error = None
            for attempt in range(10):
                candidate = self._generate_candidate_name(attempt)
                self.table_name = candidate
                try:
                    validate_table_name(self.table_name)
                    break
                except IntegrityError as e:
                    last_error = e
                    continue
            if last_error:
                raise ValidationError("无法生成唯一表名，请更换模型名称") from last_error

        # 基础校验
        validate_table_name(self.table_name)
        validate_python_identifier(self.name)

        # 保存主记录
        super().save(*args, **kwargs)

        # 自动同步到动态模型注册表（仅当启用时）
        if self.is_active:
            self.sync_to_dynamic_registry(create_table=True)

    def clean(self):
        super().clean()
        # 校验模型名称是否与已有动态模型冲突
        if self.name in _DYNAMIC_MODEL_REGISTRY and not self.pk:
            raise ValidationError(f"模型名称 '{self.name}' 已存在于动态模型注册表中")

# ========== 动态模型元数据 ==========
class FieldModel(models.Model):
    """动态模型字段配置模型"""
    # 新增外键（关联 ModelLowCode）
    model_config = models.ForeignKey(
        ModelLowCode,
        on_delete=models.CASCADE,
        related_name="fields",
        verbose_name="所属模型配置",
        null=True,  # 兼容已有数据
        blank=True
    )
    model_name = models.CharField(verbose_name="模型名称", max_length=100, default="default_model")
    name = models.CharField(verbose_name="字段名", max_length=100)
    label = models.CharField(verbose_name="字段标签", max_length=200)
    # ✅ 修复：max_length 调整为30（覆盖22字符的最长choices值）
    type = models.CharField(
        verbose_name="字段类型",
        max_length=30,
        choices=FIELD_TYPES,
        default="char"
    )
    required = models.BooleanField(verbose_name="是否必填", default=True)
    help_text = models.CharField(verbose_name="帮助文本", max_length=500, blank=True)
    options = models.TextField(verbose_name="选项配置", blank=True)  # 存储JSON格式的选项

    class Meta:
        verbose_name = "字段配置"
        verbose_name_plural = "字段配置"
        unique_together = ("model_name", "name")  # 模型+字段名唯一

    def __str__(self):
        return f"{self.model_name}.{self.name}"



class MethodLowCode(models.Model):
    """动态方法配置：为动态模型绑定自定义业务逻辑"""
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
    CUSTOM_FUNC_WHITELIST_PREFIXES = getattr(
        settings,
        "LOWCODE_FUNC_WHITELIST",
        [
            "lowcode.methods.",
            "myproject.custom_funcs.",
        ]
    )

    method_name = models.CharField(
        max_length=64,
        verbose_name="自定义方法名",
        db_index=True,
        validators=[validate_python_identifier]
    )
    model_name = models.CharField(
        max_length=64,
        verbose_name="动态模型类名",
        db_index=True,
        validators=[validate_python_identifier],
        help_text="必须与动态模型名称一致"
    )
    logic_type = models.CharField(
        max_length=32,
        verbose_name="逻辑模板类型",
        choices=[
            ("aggregate", "聚合计算"),
            ("field_update", "字段更新"),
            ("custom_func", "自定义函数")
        ]
    )
    params = models.JSONField(verbose_name="方法参数配置")
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
        indexes = [
            models.Index(fields=["model_name", "logic_type", "is_active"]),
            models.Index(fields=["custom_func_path", "is_active"]),
        ]

    def __str__(self) -> str:
        status = "✅" if self.is_active else "❌"
        return f"{status} {self.model_name}.{self.method_name} ({self.logic_type})"

    def clean(self):
        super().clean()
        if not self.logic_type:
            raise ValidationError("必须指定逻辑模板类型")

        # 校验模型是否存在
        if self.model_name not in _DYNAMIC_MODEL_REGISTRY:
            raise ValidationError(f"动态模型 '{self.model_name}' 不存在，请先创建模型")

        # 校验参数schema
        schema = self.LOGIC_TYPE_SCHEMAS.get(self.logic_type)
        if not schema:
            raise ValidationError(f"不支持的逻辑类型: {self.logic_type}（支持：{list(self.LOGIC_TYPE_SCHEMAS.keys())}）")

        params = self.params or {}
        if not isinstance(params, dict):
            raise ValidationError("参数配置必须是JSON对象")

        # 校验必填参数
        missing = [k for k in schema["required"] if k not in params]
        if missing:
            raise ValidationError(
                f"逻辑类型'{self.logic_type}'要求参数包含：{missing}（当前缺失）"
            )

        # 聚合操作校验
        if self.logic_type == "aggregate":
            op = str(params.get("operation", "sum")).lower()
            allowed_ops = {"sum", "avg", "count", "max", "min"}
            if op not in allowed_ops:
                raise ValidationError(
                    f"聚合操作'{op}'不受支持，允许值：{sorted(allowed_ops)}"
                )

        # 自定义函数安全校验
        if self.logic_type == "custom_func":
            func_path = params.get("func_path", "").strip()
            if not func_path or "." not in func_path:
                raise ValidationError("自定义函数路径格式应为 'module.submodule.func_name'")
            if not any(func_path.startswith(prefix) for prefix in self.CUSTOM_FUNC_WHITELIST_PREFIXES):
                raise ValidationError(
                    f"自定义函数路径必须以白名单前缀开头：{self.CUSTOM_FUNC_WHITELIST_PREFIXES}"
                )
            self.custom_func_path = func_path
        else:
            self.custom_func_path = ""

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)