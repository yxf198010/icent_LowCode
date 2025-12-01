# utils/json_utils.py
import json
from typing import Any, List


def parse_json_array(raw: str) -> List[Any]:
    """
    安全解析 JSON 字符串为列表
    抛出 ValueError 或 TypeError 表示无效
    """
    if not raw or not raw.strip():
        raise ValueError("JSON 内容为空")
    data = json.loads(raw)
    if not isinstance(data, list):
        raise TypeError("JSON 根元素必须是数组")
    return data


def format_json_for_storage(data: list) -> str:
    """将字段配置列表格式化为美观、可读的 JSON 字符串用于存储"""
    return json.dumps(data, ensure_ascii=False, indent=2)