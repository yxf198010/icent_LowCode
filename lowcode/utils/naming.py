# utils/naming.py
import re


def generate_table_name_from_model(model_name: str) -> str:
    """根据模型类名生成默认数据库表名"""
    return f'lowcode_{model_name.lower()}'


def is_valid_python_class_name(name: str) -> bool:
    """检查是否为合法的 Python 类名（首字母大写，仅字母数字下划线）"""
    return bool(re.match(r'^[A-Z][a-zA-Z0-9_]*$', name))


def is_valid_db_table_name(name: str) -> bool:
    """检查是否为合法的数据库表名（字母/下划线开头，仅字母数字下划线）"""
    return bool(re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name))


def is_valid_field_name(name: str) -> bool:
    """检查是否为合法的模型字段名（小写字母开头，仅字母数字下划线）"""
    return bool(re.match(r'^[a-z][a-z0-9_]*$', name))