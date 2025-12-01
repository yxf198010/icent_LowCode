# lowcode/api/serializers.py
"""
低代码平台 API 序列化器集合

包含四类核心功能：
1. 动态表创建（DynamicTableCreateSerializer）
2. 动态模型字段升级（UpgradeModelSerializer）
3. 方法调用日志查询（LowCodeMethodCallLogSerializer）
4. 数据权限批量授权与撤销（BatchDataPermissionSerializer, BatchRevokeDataPermissionSerializer）

所有输入均经过严格校验，防止注入、保留字冲突、非法类型等风险。
"""

import re
from typing import Any, Dict, List, Optional
from rest_framework import serializers
from django.conf import settings
from django.db.models import Q

# ========================
# 全局常量（共享）
# ========================

# 支持的字段类型（用于 UpgradeModel）
SUPPORTED_FIELD_TYPES = {
    "string", "text", "integer", "float", "boolean",
    "date", "datetime", "email", "url", "json",
    "foreignkey", "choice", "decimal"
}

# 系统保留字段名（禁止用户使用）
RESERVED_FIELD_NAMES = {
    "id", "pk", "objects", "save", "delete", "refresh_from_db",
    "_state", "_meta", "serializable_value", "get_deferred_fields",
    "DoesNotExist", "MultipleObjectsReturned", "base_manager", "manager",
    "__str__", "__repr__", "__init__", "__eq__", "__hash__"
}


# ========================
# 通用校验工具函数（共享）
# ========================

def validate_identifier_name(name: str, max_length: int = 64, context: str = "标识符") -> str:
    """
    通用标识符校验（适用于表名、字段名、模型名等）
    要求：以字母/下划线开头，仅含字母、数字、下划线、连字符（-），长度限制
    """
    if not isinstance(name, str):
        raise serializers.ValidationError(f"{context}必须是字符串")
    name = name.strip()
    if not name:
        raise serializers.ValidationError(f"{context}不能为空")

    # 允许下划线、连字符，但首字符不能为数字或连字符（Django 模型/表惯例）
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_-]*$", name):
        raise serializers.ValidationError(
            f"{context}必须以字母或下划线开头，仅包含字母、数字、下划线或连字符"
        )
    if name[0].isdigit():
        raise serializers.ValidationError(f"{context}不能以数字开头")
    if len(name) > max_length:
        raise serializers.ValidationError(f"{context}长度不能超过 {max_length} 个字符")

    return name


def validate_field_name(name: str) -> str:
    """校验字段名是否合法（额外检查保留字和 magic method）"""
    name = validate_identifier_name(name, max_length=64, context="字段名")

    if name in RESERVED_FIELD_NAMES:
        raise serializers.ValidationError(f"字段名 '{name}' 是系统保留字，禁止使用")
    if name.startswith("__") or name.endswith("__"):
        raise serializers.ValidationError("字段名不能以双下划线开头或结尾（避免与 magic method 冲突）")

    return name


# ========================
# 字段定义校验（用于 UpgradeModel）
# ========================

def _validate_string_options(options: dict) -> None:
    max_len = options.get("max_length")
    if max_len is not None:
        if not isinstance(max_len, int) or max_len <= 0 or max_len > 10000:
            raise serializers.ValidationError(
                "string 类型的 max_length 必须是 1~10000 之间的整数"
            )


def _validate_choice_options(options: dict) -> None:
    choices = options.get("choices")
    if not choices or not isinstance(choices, list):
        raise serializers.ValidationError("choice 类型必须提供非空列表 'choices'")
    if len(choices) > 100:
        raise serializers.ValidationError("choice 选项数量不能超过 100 项")
    if not all(isinstance(c, (str, int)) for c in choices):
        raise serializers.ValidationError("choices 中的值必须是字符串或整数")
    if len(set(choices)) != len(choices):
        raise serializers.ValidationError("choices 中存在重复值")


def _validate_foreignkey_options(options: dict) -> None:
    target = options.get("target_model")
    if not target or not isinstance(target, str):
        raise serializers.ValidationError("foreignkey 必须指定 'target_model'（字符串，如 'User' 或 'lowcode.Order'）")
    if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_.]*$", target):
        raise serializers.ValidationError("target_model 格式无效，应为 'ModelName' 或 'app.ModelName'")


def _validate_decimal_options(options: dict) -> None:
    max_digits = options.get("max_digits")
    decimal_places = options.get("decimal_places")
    if not isinstance(max_digits, int) or not isinstance(decimal_places, int):
        raise serializers.ValidationError("decimal 类型需提供整数类型的 'max_digits' 和 'decimal_places'")
    if max_digits <= 0 or decimal_places < 0 or decimal_places >= max_digits:
        raise serializers.ValidationError(
            "'max_digits' 必须大于 0，'decimal_places' 必须 >=0 且 < max_digits"
        )
    if max_digits > 50:
        raise serializers.ValidationError("'max_digits' 不能超过 50")


def validate_field_definition(field: Dict[str, Any]) -> Dict[str, Any]:
    """校验单个字段定义的结构与内容（用于 UpgradeModel）"""
    if not isinstance(field, dict):
        raise serializers.ValidationError("字段定义必须是对象（字典）")

    name = field.get("name")
    field_type = field.get("type")
    options = field.get("options", {})

    if name is None:
        raise serializers.ValidationError("字段缺少 'name'")
    if field_type is None:
        raise serializers.ValidationError("字段缺少 'type'")

    validated_name = validate_field_name(str(name))
    field["name"] = validated_name

    if field_type not in SUPPORTED_FIELD_TYPES:
        raise serializers.ValidationError(
            f"不支持的字段类型 '{field_type}'。支持类型: {', '.join(sorted(SUPPORTED_FIELD_TYPES))}"
        )

    if not isinstance(options, dict):
        raise serializers.ValidationError("'options' 必须是字典")

    try:
        if field_type == "string":
            _validate_string_options(options)
        elif field_type == "choice":
            _validate_choice_options(options)
        elif field_type == "foreignkey":
            _validate_foreignkey_options(options)
        elif field_type == "decimal":
            _validate_decimal_options(options)
    except serializers.ValidationError:
        raise
    except Exception as e:
        raise serializers.ValidationError(f"字段选项校验异常: {str(e)}")

    return field


# ========================
# 序列化器 1：动态表创建
# ========================

class DynamicTableCreateSerializer(serializers.Serializer):
    """
    用于通过样例数据动态创建数据库表（低代码前端调用）
    """

    table_name = serializers.CharField(
        max_length=63,
        help_text="表名（字母、数字、下划线或连字符，不超过63字符）"
    )
    sample_data = serializers.DictField(
        child=serializers.JSONField(),
        help_text="样例数据，用于自动推断字段类型"
    )
    primary_key = serializers.ListField(
        child=serializers.CharField(max_length=63),
        required=False,
        allow_empty=True,
        help_text="主键字段列表（留空则自动检测 id / {table}_id）"
    )
    indexes = serializers.ListField(
        child=serializers.ListField(child=serializers.CharField(max_length=63)),
        required=False,
        default=list,
        help_text='索引定义，如 [["user_id"], ["status", "created_at"]]'
    )
    database_alias = serializers.CharField(
        default="default",
        help_text="Django DATABASES 中的连接别名"
    )

    def validate_table_name(self, value: str) -> str:
        return validate_identifier_name(value, max_length=63, context="表名")

    def validate_sample_data(self, value: dict) -> dict:
        if not value:
            raise serializers.ValidationError("sample_data 不能为空")
        for key in value.keys():
            # 复用字段名校验逻辑（但允许作为普通字段名，不检查 RESERVED_FIELD_NAMES）
            # 因为动态表可能包含 id/user_id 等常见名
            if not isinstance(key, str):
                raise serializers.ValidationError("字段名必须是字符串")
            # 使用宽松版校验（允许 id 等）
            if not re.match(r"^[a-zA-Z_][a-zA-Z0-9_-]*$", key):
                raise serializers.ValidationError(f"无效字段名: {key}")
            if key[0].isdigit():
                raise serializers.ValidationError(f"字段名不能以数字开头: {key}")
        return value

    def validate_database_alias(self, value: str) -> str:
        if value not in settings.DATABASES:
            raise serializers.ValidationError(f"未知的数据库别名: {value}")
        return value


# ========================
# 序列化器 2：动态模型字段升级
# ========================

class UpgradeModelSerializer(serializers.Serializer):
    """
    动态模型升级请求序列化器。
    用于接收前端传入的新字段定义，执行模型结构变更。
    """

    model_name = serializers.CharField(max_length=100, help_text="动态模型名称（如 Order）")
    fields = serializers.JSONField(help_text="字段定义列表，每个字段包含 name/type/options")
    no_backup = serializers.BooleanField(default=False, help_text="跳过备份（不推荐）")
    no_restart = serializers.BooleanField(default=False, help_text="不重启服务（仅开发环境可用）")
    force = serializers.BooleanField(default=False, help_text="强制执行（可能丢失数据）")

    def validate_model_name(self, value: str) -> str:
        return validate_identifier_name(value, max_length=100, context="模型名")

    def validate_fields(self, value: List[Any]) -> List[Dict[str, Any]]:
        if not isinstance(value, list):
            raise serializers.ValidationError("fields 必须是一个列表")
        if not value:
            raise serializers.ValidationError("字段列表不能为空")
        if len(value) > 50:
            raise serializers.ValidationError("单次升级最多支持 50 个字段")

        validated_fields = []
        seen_names = set()

        for idx, field in enumerate(value):
            try:
                validated_field = validate_field_definition(field)
                name = validated_field["name"]
                if name in seen_names:
                    raise serializers.ValidationError(f"字段名 '{name}' 在列表中重复")
                seen_names.add(name)
                validated_fields.append(validated_field)
            except serializers.ValidationError as e:
                raise serializers.ValidationError(
                    f"字段 #{idx + 1}（name='{field.get('name', 'N/A')}'）校验失败: {e.detail}"
                )

        return validated_fields

    def validate(self, attrs):
        # 可扩展：根据环境限制危险操作
        # 示例：
        # if not settings.DEBUG and (attrs['force'] or attrs['no_restart']):
        #     raise serializers.ValidationError("生产环境禁止使用 force 或 no_restart 参数")
        return attrs


# ========================
# 序列化器 3：方法调用日志查询
# ========================

# 延迟导入模型，避免循环依赖（也可放在顶部，若无循环问题）
from ..models import LowCodeMethodCallLog, DataPermission  # noqa


class LowCodeMethodCallLogSerializer(serializers.ModelSerializer):
    """日志查询序列化器"""
    username = serializers.CharField(source="user.username", read_only=True)
    call_time = serializers.DateTimeField(format="%Y-%m-%d %H:%M:%S", read_only=True)

    class Meta:
        model = LowCodeMethodCallLog
        fields = [
            "id", "username", "model_name", "method_name",
            "params", "result_status", "result_data",
            "exception_msg", "call_time", "time_cost"
        ]


# ========================
# 序列化器 4：数据权限批量操作
# ========================

class BatchDataPermissionSerializer(serializers.Serializer):
    """批量数据权限授权序列化器"""
    model_name = serializers.CharField(max_length=64, required=True, label="模型类名")
    user_ids = serializers.ListField(child=serializers.IntegerField(), required=True, label="授权用户ID列表")
    data_ids = serializers.ListField(child=serializers.CharField(), required=True, label="授权数据ID列表")

    def validate(self, data):
        """额外校验：避免重复授权（用户-模型-数据ID唯一）"""
        model_name = data["model_name"]
        user_ids = data["user_ids"]
        data_ids = data["data_ids"]

        # 构建重复条件
        duplicate_conditions = Q()
        for user_id in user_ids:
            for data_id in data_ids:
                duplicate_conditions |= Q(user_id=user_id, model_name=model_name, data_id=data_id)

        duplicate_count = DataPermission.objects.filter(duplicate_conditions).count()
        if duplicate_count > 0:
            raise serializers.ValidationError(f"存在{duplicate_count}条重复授权，请检查后重试")

        return data


class BatchRevokeDataPermissionSerializer(serializers.Serializer):
    """批量撤销数据权限序列化器"""
    model_name = serializers.CharField(max_length=64, required=False, label="模型类名")
    user_ids = serializers.ListField(child=serializers.IntegerField(), required=False, label="用户ID列表")
    data_ids = serializers.ListField(child=serializers.CharField(), required=False, label="数据ID列表")

    def validate(self, data):
        """校验：至少传一种有效组合"""
        user_ids = data.get("user_ids", [])
        model_name = data.get("model_name")
        data_ids = data.get("data_ids", [])

        has_valid_comb = (
            (user_ids and model_name) or
            (user_ids and data_ids) or
            (model_name and data_ids)
        )
        if not has_valid_comb:
            raise serializers.ValidationError(
                "请至少传入一种有效组合：user_ids+model_name / user_ids+data_ids / model_name+data_ids"
            )
        return data