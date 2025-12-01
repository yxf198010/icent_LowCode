"""
动态模型配置文件存储模块：提供模型定义的持久化功能。
支持线程安全读写、原子写入、路径自定义、日志记录等生产级特性。
"""

import json
import os
import logging
import threading
import tempfile
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

# 支持通过环境变量自定义存储路径（优先级最高）
STORAGE_FILE = os.environ.get(
    'LOWCODE_STORAGE_FILE',
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dynamic_models.json")
)

# 线程锁：防止并发写入冲突（注意：多进程需依赖文件系统原子性）
_FILE_LOCK = threading.RLock()

# 配置文件版本（用于未来兼容性）
CONFIG_VERSION = "1.0"

# 字段配置允许的顶层键（与 engine.py 保持一致）
ALLOWED_FIELD_KEYS = {
    "name",
    "type",
    "verbose_name",
    "help_text",
    "null",
    "blank",
    "default",
    "unique",
    "max_length",
    "max_digits",
    "decimal_places",
    "auto_now",
    "auto_now_add",
}


def _ensure_dir_exists(filepath: str) -> None:
    """确保文件所在目录存在"""
    directory = os.path.dirname(filepath)
    if directory and not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)


def _atomic_write(filepath: str, data: dict) -> None:
    """
    原子写入 JSON 文件，避免写入中断导致文件损坏。
    """
    _ensure_dir_exists(filepath)
    with _FILE_LOCK:
        # 写入临时文件
        with tempfile.NamedTemporaryFile(
                mode='w',
                encoding='utf-8',
                dir=os.path.dirname(filepath),
                delete=False,
                suffix='.tmp'
        ) as tmp_file:
            json.dump(data, tmp_file, ensure_ascii=False, indent=2)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
            temp_name = tmp_file.name

        # 原子替换（POSIX 系统上 rename 是原子的）
        try:
            os.replace(temp_name, filepath)
        except OSError:
            # 回退到普通 rename（Windows 兼容）
            os.rename(temp_name, filepath)


def _validate_field_config(field: Dict[str, Any], model_name: str) -> bool:
    """
    校验单个字段配置是否合法。
    返回 True 表示有效，False 表示应跳过或报错。
    """
    if not isinstance(field, dict):
        logger.error(f"[ERROR] 模型 '{model_name}' 包含非字典字段: {field}")
        return False

    name = field.get("name")
    field_type = field.get("type")

    if not name or not isinstance(name, str):
        logger.error(f"[ERROR] 模型 '{model_name}' 的字段缺少有效 'name': {field}")
        return False

    if not field_type or not isinstance(field_type, str):
        logger.error(f"[ERROR] 模型 '{model_name}' 的字段 '{name}' 缺少 'type'")
        return False

    # 检查非法键（防御注入）
    invalid_keys = set(field.keys()) - ALLOWED_FIELD_KEYS
    if invalid_keys:
        logger.warning(f"[WARNING] 模型 '{model_name}' 字段 '{name}' 包含未识别参数: {invalid_keys}")

    return True


def save_model_config(
    model_name: str,
    fields: List[Dict[str, Any]],
    table_name: Optional[str] = None
) -> bool:
    """
    保存动态模型配置到文件（支持完整字段参数）

    Args:
        model_name (str): 模型名称
        fields (List[Dict]): 字段配置列表
        table_name (Optional[str]): 数据表名（若未提供，调用方应自行处理）

    Returns:
        bool: 保存是否成功
    """
    if not isinstance(model_name, str) or not model_name.strip():
        logger.error("[ERROR] 模型名称不能为空")
        return False

    if not isinstance(fields, list):
        logger.error(f"[ERROR] 模型 '{model_name}' 的字段配置必须是列表")
        return False

    model_name = model_name.strip()

    # 校验每个字段
    validated_fields = []
    for field in fields:
        if _validate_field_config(field, model_name):
            # 只保留已知安全的键
            clean_field = {k: v for k, v in field.items() if k in ALLOWED_FIELD_KEYS}
            validated_fields.append(clean_field)
        else:
            # 校验失败的字段直接拒绝保存（避免污染配置）
            return False

    try:
        configs = load_all_model_configs()

        config_entry = {
            "fields": validated_fields,
            "version": CONFIG_VERSION,
        }
        if table_name is not None:
            if not isinstance(table_name, str) or not table_name.strip():
                logger.warning(f"[WARNING] 模型 '{model_name}' 的 table_name 为空，将不保存")
            else:
                config_entry["table_name"] = table_name.strip()

        configs[model_name] = config_entry

        _atomic_write(STORAGE_FILE, configs)

        logger.info(f"[OK] 已保存模型配置: {model_name} (表: {config_entry.get('table_name', 'auto')})")
        return True

    except Exception as e:
        logger.error(f"[ERROR] 保存模型配置失败: {e}", exc_info=True)
        return False


def load_all_model_configs() -> Dict[str, Dict[str, Any]]:
    """
    加载所有已保存的模型配置

    Returns:
        Dict[str, Dict]: 模型名 -> 配置字典
    """
    if not os.path.exists(STORAGE_FILE):
        logger.debug(f"[DEBUG] 配置文件不存在，将创建新文件: {STORAGE_FILE}")
        return {}

    try:
        with open(STORAGE_FILE, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
            if not isinstance(raw_data, dict):
                logger.error(f"[ERROR] 配置文件根节点必须是 JSON 对象: {STORAGE_FILE}")
                return {}

            # 安全加载：逐个验证模型配置
            safe_configs = {}
            for model_name, config in raw_data.items():
                if not isinstance(model_name, str) or not model_name:
                    logger.warning(f"[WARNING] 跳过无效模型名: {model_name}")
                    continue
                if not isinstance(config, dict):
                    logger.warning(f"[WARING] 模型 '{model_name}' 配置不是对象，跳过")
                    continue

                fields = config.get("fields", [])
                if not isinstance(fields, list):
                    logger.warning(f"[WARNING] 模型 '{model_name}' 的 'fields' 不是列表，跳过")
                    continue

                # 可选：未来可在此做版本迁移
                safe_configs[model_name] = config

            return safe_configs

    except json.JSONDecodeError as e:
        logger.error(f"[ERROR] JSON 解析失败 ({STORAGE_FILE}): {e}")
        return {}
    except Exception as e:
        logger.error(f"[ERROR] 读取配置文件失败: {e}", exc_info=True)
        return {}


def get_model_config(model_name: str) -> Optional[Dict[str, Any]]:
    """
    获取单个模型配置

    Args:
        model_name (str): 模型名称

    Returns:
        Optional[Dict]: 配置字典，不存在则返回 None
    """
    if not isinstance(model_name, str):
        return None
    model_name = model_name.strip()
    if not model_name:
        return None

    configs = load_all_model_configs()
    config = configs.get(model_name)
    if config is None:
        logger.debug(f"[DEBUG] 未找到模型配置: {model_name}")
    return config


def delete_model_config(model_name: str) -> bool:
    """
    删除模型配置

    Args:
        model_name (str): 模型名称

    Returns:
        bool: 删除是否成功（不存在视为成功）
    """
    if not isinstance(model_name, str) or not model_name.strip():
        logger.error("[ERROR] 模型名称无效")
        return False

    model_name = model_name.strip()

    try:
        configs = load_all_model_configs()

        if model_name not in configs:
            logger.debug(f"[DEBUG] 模型配置不存在，无需删除: {model_name}")
            return True

        del configs[model_name]

        _atomic_write(STORAGE_FILE, configs)

        logger.info(f"OK 已删除模型配置: {model_name}")
        return True

    except Exception as e:
        logger.error(f"[ERROR] 删除模型配置失败: {e}", exc_info=True)
        return False


def get_storage_file_path() -> str:
    """获取当前存储文件路径（用于调试）"""
    return os.path.abspath(STORAGE_FILE)


def backup_storage_file() -> Optional[str]:
    """
    创建配置文件备份（用于高危操作前）
    返回备份路径，失败返回 None
    """
    if not os.path.exists(STORAGE_FILE):
        return None

    backup_path = f"{STORAGE_FILE}.bak"
    try:
        import shutil
        shutil.copy2(STORAGE_FILE, backup_path)
        logger.info(f"[OK] 已创建备份: {backup_path}")
        return backup_path
    except Exception as e:
        logger.error(f"[ERROR] 备份失败: {e}")
        return None