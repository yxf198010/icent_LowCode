# lowcode/utils/dynamic_model_loader.py
"""
动态模型相关工具函数（命名校验、唯一性检查等）
注意：此文件目前不涉及实际动态模型生成，仅提供表单校验支持。
"""

from typing import Optional
from lowcode.models.models import LowCodeModelConfig


def is_model_name_unique(name: str, exclude_id: Optional[int] = None) -> bool:
    """
    检查模型名称是否唯一（排除自身）
    """
    queryset = LowCodeModelConfig.objects.filter(name=name)
    if exclude_id is not None:
        queryset = queryset.exclude(pk=exclude_id)
    return not queryset.exists()


def is_table_name_unique(table_name: str, exclude_id: Optional[int] = None) -> bool:
    """
    检查数据库表名是否唯一（排除自身）
    """
    queryset = LowCodeModelConfig.objects.filter(table_name=table_name)
    if exclude_id is not None:
        queryset = queryset.exclude(pk=exclude_id)
    return not queryset.exists()


def ensure_unique_table_name(base: str, exclude_id: Optional[int] = None) -> str:
    """
    确保生成的表名唯一，若冲突则追加数字后缀（如 lowcode_user_1）
    """
    original = base
    counter = 1
    while True:
        if is_table_name_unique(base, exclude_id):
            return base
        base = f"{original}_{counter}"
        counter += 1