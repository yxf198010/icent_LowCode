# lowcode/core/ddl_executor.py
"""
安全执行 DDL 操作（CREATE/DROP TABLE, ADD/DROP COLUMN）
使用 RawMultiTableTransaction + 参数化标识符，避免 SQL 注入
"""

import logging
from typing import Dict, Any, Optional, List
from psycopg2 import sql as pg_sql
import sqlite3
from lowcode.core.raw_transaction import RawMultiTableTransaction
from lowcode.utils.db_config import get_db_config

logger = logging.getLogger(__name__)


def _get_identifier_quoter(db_vendor: str):
    """
    返回对应数据库的标识符引用函数（用于表名、字段名转义）
    """
    if db_vendor == "postgresql":
        return pg_sql.Identifier
    elif db_vendor in ("mysql", "mariadb"):
        # MySQL 使用反引号
        class MySQLIdentifier:
            def __init__(self, name):
                self.name = name

            def as_string(self, cursor):
                return f"`{self.name}`"

        return MySQLIdentifier
    elif db_vendor == "sqlite":
        # SQLite 使用双引号
        class SQLiteIdentifier:
            def __init__(self, name):
                self.name = name

            def as_string(self, cursor):
                return f'"{self.name}"'

        return SQLiteIdentifier
    else:
        raise ValueError(f"Unsupported database vendor: {db_vendor}")


def _build_column_def(field_def: Dict[str, Any], db_vendor: str) -> str:
    """
    根据字段定义生成列定义字符串（如 "name VARCHAR(255) NOT NULL"）
    注意：此处假设 field_def 包含 type 和 params（如 max_length, null 等）

    ⚠️ 此函数需根据你的 FIELD_TYPE_MAP 实际映射调整！
    """
    field_type = field_def["type"]
    params = field_def.get("params", {})

    # === 示例类型映射（请根据你的 FIELD_TYPE_MAP 调整）===
    TYPE_MAP = {
        "CharField": "VARCHAR(%(max_length)s)",
        "TextField": "TEXT",
        "IntegerField": "INTEGER",
        "BigIntegerField": "BIGINT",
        "BooleanField": "BOOLEAN" if db_vendor == "postgresql" else "TINYINT(1)",
        "FloatField": "REAL",
        "DecimalField": "DECIMAL(%(max_digits)s, %(decimal_places)s)",
        "DateTimeField": "TIMESTAMP",
        "DateField": "DATE",
        "AutoField": "SERIAL" if db_vendor == "postgresql" else "INTEGER AUTO_INCREMENT PRIMARY KEY",
    }

    base_type = TYPE_MAP.get(field_type)
    if not base_type:
        raise ValueError(f"Unsupported field type for DDL: {field_type}")

    # 渲染类型（如 VARCHAR(255)）
    try:
        col_type = base_type % params
    except KeyError as e:
        raise ValueError(f"Missing parameter for {field_type}: {e}")

    # 处理 NULL / NOT NULL
    null_clause = "NULL" if params.get("null", False) else "NOT NULL"

    # 处理默认值（简单字符串/数字，复杂默认值需特殊处理）
    default = params.get("default")
    default_clause = ""
    if default is not None and default != "":
        if isinstance(default, str):
            # 转义字符串（简单处理，生产建议用参数化或限制默认值类型）
            escaped_default = default.replace("'", "''")
            default_clause = f" DEFAULT '{escaped_default}'"
        elif isinstance(default, (int, float)):
            default_clause = f" DEFAULT {default}"
        # 注意：不支持函数默认值（如 NOW()），需扩展

    return f"{col_type} {null_clause}{default_clause}".strip()


def create_table_if_not_exists(
        table_name: str,
        fields: List[Dict[str, Any]],
        db_alias: str = "default"
) -> bool:
    """
    安全创建表（IF NOT EXISTS）

    Args:
        table_name: 表名（将被安全转义）
        fields: 字段定义列表，每个 dict 含 'name', 'type', 'params'
        db_alias: 数据库别名（用于获取连接配置）

    Returns:
        bool: 是否成功
    """
    config = get_db_config(db_alias)
    vendor = config["vendor"]

    def ddl_logic(cursor, _):
        Identifier = _get_identifier_quoter(vendor)

        # 构建列定义
        columns = []
        primary_keys = []
        for field in fields:
            col_name = field["name"]
            col_def = _build_column_def(field, vendor)
            # 特殊处理主键（示例：若字段名为 'id' 且是 AutoField）
            if field["type"] == "AutoField" or field.get("primary_key"):
                if vendor == "sqlite":
                    col_def = col_def.replace("INTEGER", "INTEGER PRIMARY KEY")
                elif vendor in ("mysql", "mariadb"):
                    col_def += " PRIMARY KEY"
                # PostgreSQL SERIAL 已隐含主键
            columns.append(f"{Identifier(col_name).as_string(cursor)} {col_def}")

        # 组装 CREATE TABLE 语句
        if vendor == "postgresql":
            stmt = f"CREATE TABLE IF NOT EXISTS {Identifier(table_name).as_string(cursor)} ({', '.join(columns)})"
        else:
            # MySQL / SQLite
            stmt = f"CREATE TABLE IF NOT EXISTS {Identifier(table_name).as_string(cursor)} ({', '.join(columns)})"

        logger.info(f"Executing DDL: {stmt}")
        cursor.execute(stmt)

    try:
        tx = RawMultiTableTransaction(config)
        tx.execute(ddl_logic)
        logger.info(f"✅ Table '{table_name}' created successfully.")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to create table '{table_name}': {e}", exc_info=True)
        return False


def drop_table_if_exists(table_name: str, db_alias: str = "default") -> bool:
    """安全删除表（IF EXISTS）"""
    config = get_db_config(db_alias)
    vendor = config["vendor"]

    def ddl_logic(cursor, _):
        Identifier = _get_identifier_quoter(vendor)
        stmt = f"DROP TABLE IF EXISTS {Identifier(table_name).as_string(cursor)}"
        logger.info(f"Executing DDL: {stmt}")
        cursor.execute(stmt)

    try:
        tx = RawMultiTableTransaction(config)
        tx.execute(ddl_logic)
        logger.info(f"🗑️ Table '{table_name}' dropped successfully.")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to drop table '{table_name}': {e}", exc_info=True)
        return False


def add_column(
        table_name: str,
        field_def: Dict[str, Any],
        db_alias: str = "default"
) -> bool:
    """添加列（ALTER TABLE ... ADD COLUMN）"""
    config = get_db_config(db_alias)
    vendor = config["vendor"]

    def ddl_logic(cursor, _):
        Identifier = _get_identifier_quoter(vendor)
        col_name = field_def["name"]
        col_def = _build_column_def(field_def, vendor)
        stmt = (
            f"ALTER TABLE {Identifier(table_name).as_string(cursor)} "
            f"ADD COLUMN {Identifier(col_name).as_string(cursor)} {col_def}"
        )
        logger.info(f"Executing DDL: {stmt}")
        cursor.execute(stmt)

    try:
        tx = RawMultiTableTransaction(config)
        tx.execute(ddl_logic)
        logger.info(f"➕ Column '{field_def['name']}' added to '{table_name}'.")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to add column '{field_def['name']}': {e}", exc_info=True)
        return False


def drop_column(table_name: str, column_name: str, db_alias: str = "default") -> bool:
    """删除列（ALTER TABLE ... DROP COLUMN）"""
    config = get_db_config(db_alias)
    vendor = config["vendor"]

    # SQLite 不支持 DROP COLUMN（需重建表）
    if vendor == "sqlite":
        logger.error("SQLite does not support DROP COLUMN. Operation aborted.")
        return False

    def ddl_logic(cursor, _):
        Identifier = _get_identifier_quoter(vendor)
        stmt = (
            f"ALTER TABLE {Identifier(table_name).as_string(cursor)} "
            f"DROP COLUMN {Identifier(column_name).as_string(cursor)}"
        )
        logger.info(f"Executing DDL: {stmt}")
        cursor.execute(stmt)

    try:
        tx = RawMultiTableTransaction(config)
        tx.execute(ddl_logic)
        logger.info(f"🗑️ Column '{column_name}' dropped from '{table_name}'.")
        return True
    except Exception as e:
        logger.error(f"❌ Failed to drop column '{column_name}': {e}", exc_info=True)
        return False