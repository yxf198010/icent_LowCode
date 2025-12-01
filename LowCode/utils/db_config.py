# lowcode/utils/db_config.py
"""
从 Django settings 中提取并标准化数据库连接配置
"""
# 在 ddl_executor.py 或其他模块中
# from lowcode.utils.db_config import get_db_config
#
# config = get_db_config("default")
# print(config)
# # 示例输出（PostgreSQL）:
# # {
# #   'vendor': 'postgresql',
# #   'host': 'localhost',
# #   'port': 5432,
# #   'database': 'myapp_db',
# #   'user': 'myuser',
# #   'password': 'secret'
# # }
from django.conf import settings
from typing import Dict, Any


def get_db_config(alias: str = "default") -> Dict[str, Any]:
    """
    获取指定数据库别名的连接配置，并标准化为统一格式。

    返回字典包含以下键：
        - host: str
        - port: int
        - database: str
        - user: str
        - password: str (可能为空)
        - vendor: str ("postgresql", "mysql", "sqlite", "mariadb")

    Args:
        alias (str): 数据库别名，默认为 "default"

    Returns:
        dict: 标准化后的数据库配置

    Raises:
        KeyError: 如果 DATABASES 中不存在该 alias
        ValueError: 如果 ENGINE 无法识别
    """
    if alias not in settings.DATABASES:
        raise KeyError(f"Database alias '{alias}' not found in settings.DATABASES")

    db_config = settings.DATABASES[alias]
    engine = db_config.get("ENGINE", "").lower()

    # 解析 vendor
    if "postgresql" in engine:
        vendor = "postgresql"
        default_port = 5432
    elif "mysql" in engine:
        vendor = "mysql"
        default_port = 3306
    elif "mariadb" in engine:
        vendor = "mariadb"
        default_port = 3306
    elif "sqlite" in engine:
        vendor = "sqlite"
        default_port = None  # SQLite 不需要端口
    else:
        raise ValueError(f"Unsupported database engine: {engine}")

    # 提取通用字段
    config = {
        "vendor": vendor,
        "host": db_config.get("HOST", "localhost") or "localhost",
        "port": int(db_config.get("PORT", default_port)) if default_port else None,
        "database": db_config["NAME"],
        "user": db_config.get("USER", ""),
        "password": db_config.get("PASSWORD", ""),
    }

    # 特殊处理 SQLite：NAME 是文件路径
    if vendor == "sqlite":
        if config["database"] == ":memory:":
            raise ValueError("In-memory SQLite databases are not supported for DDL operations.")
        # 确保路径是绝对路径（可选）
        import os
        from pathlib import Path
        if not os.path.isabs(config["database"]):
            config["database"] = str(Path(settings.BASE_DIR) / config["database"])

    return config