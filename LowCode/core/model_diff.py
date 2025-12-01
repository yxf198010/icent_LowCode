# lowcode/core/model_diff.py
"""
字段差异比较与安全校验工具（无 Django 依赖）
"""
# 示例字段定义
# old = [
#     {"name": "id", "type": "AutoField"},
#     {"name": "title", "type": "CharField", "max_length": 100, "null": False},
#     {"name": "count", "type": "IntegerField", "null": False},
# ]
#
# new = [
#     {"name": "id", "type": "AutoField"},
#     {"name": "title", "type": "TextField"},  # CharField → TextField（安全）
#     {"name": "score", "type": "FloatField"},  # 新增
#     # count 被删除（危险！）
# ]
#
# diff = diff_fields(old, new)
# print(diff)
# # {'add': [{'name': 'score', 'type': 'FloatField', 'params': {}}],
# #  'drop': ['count'],
# #  'modify': [({'name': 'title', ...}, {'name': 'title', ...})]}
#
# safe, msg = validate_field_changes(old, new)
# print(safe, msg)
# # False 删除字段 'count' 存在风险：该字段不允许为空且无默认值...
from typing import List, Dict, Any, Set, Tuple


def _normalize_field_def(field: Dict[str, Any]) -> Dict[str, Any]:
    """
    标准化字段定义，便于比较。
    移除 name/type 后其余参数视为 params。
    """
    return {
        "name": field.get("name", ""),
        "type": field.get("type", ""),
        "params": {k: v for k, v in field.items() if k not in ("name", "type")}
    }


def diff_fields(old_fields: List[Dict], new_fields: List[Dict]) -> Dict[str, List]:
    """
    比较两组字段定义的差异。

    Args:
        old_fields: 原始字段列表，每个元素为 dict，含 'name', 'type', 及其他参数
        new_fields: 新字段列表，格式同上

    Returns:
        {
            "add": [field_def, ...],          # 新增字段定义
            "drop": [field_name, ...],        # 删除的字段名
            "modify": [(old_def, new_def), ...]  # 修改的字段（旧定义, 新定义）
        }
    """
    old_dict = {f["name"]: _normalize_field_def(f) for f in old_fields}
    new_dict = {f["name"]: _normalize_field_def(f) for f in new_fields}

    old_names: Set[str] = set(old_dict.keys())
    new_names: Set[str] = set(new_dict.keys())

    add = [new_dict[name] for name in (new_names - old_names)]
    drop = list(old_names - new_names)
    modify = [
        (old_dict[name], new_dict[name])
        for name in (old_names & new_names)
        if old_dict[name] != new_dict[name]
    ]

    return {"add": add, "drop": drop, "modify": modify}


def is_field_compatible(old_type: str, new_type: str) -> bool:
    """
    判断字段类型变更是否兼容（不会导致数据丢失）。

    示例规则（可根据实际需求扩展）：
      - text → varchar(255) ❌ 不安全（可能截断）
      - int → bigint ✅ 安全
      - varchar(50) → varchar(100) ✅ 安全
      - bool → int ✅（通常可转换）

    注意：此处仅做简单类型名匹配，复杂场景需解析参数（如长度）。
    """
    # 允许的“安全升级”映射（单向）
    SAFE_UPGRADES = {
        "IntegerField": "BigIntegerField",
        "SmallIntegerField": "IntegerField",
        "CharField": "TextField",  # 扩展长度通常安全
        "BooleanField": "IntegerField",
        "FloatField": "DecimalField",
    }

    # 相同类型总是兼容
    if old_type == new_type:
        return True

    # 检查是否为安全升级
    if SAFE_UPGRADES.get(old_type) == new_type:
        return True

    # 其他情况视为不安全（保守策略）
    return False


def validate_field_changes(
    old_fields: List[Dict[str, Any]],
    new_fields: List[Dict[str, Any]]
) -> Tuple[bool, str]:
    """
    验证字段变更是否安全。

    安全规则：
      1. 修改字段类型必须兼容（通过 is_field_compatible 判断）
      2. 禁止删除非空（null=False）且无默认值的字段

    Returns:
        (is_safe: bool, reason: str)
    """
    from .model_diff import is_field_compatible  # 自引用，但模块内安全

    diff = diff_fields(old_fields, new_fields)

    # 1. 检查字段类型修改是否兼容
    for old_def, new_def in diff["modify"]:
        field_name = new_def["name"]
        old_type = old_def["type"]
        new_type = new_def["type"]
        if not is_field_compatible(old_type, new_type):
            return False, (
                f"字段 '{field_name}' 类型从 '{old_type}' 改为 '{new_type}' "
                "可能导致数据丢失或转换失败"
            )

    # 2. 检查是否删除了非空字段（危险操作）
    for field_name in diff["drop"]:
        old_field = next((f for f in old_fields if f.get("name") == field_name), None)
        if old_field:
            # 默认 null=False, blank=False
            is_nullable = old_field.get("null", False)
            has_default = "default" in old_field
            if not (is_nullable or has_default):
                return False, (
                    f"删除字段 '{field_name}' 存在风险：该字段不允许为空且无默认值，"
                    "可能导致现有数据无法加载"
                )

    return True, "字段变更安全"