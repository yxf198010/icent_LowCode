# lowcode/utils/table_manager.py
"""
动态表管理工具：根据样例数据自动创建数据库表（PostgreSQL / MySQL）

适用于低代码场景中首次写入前自动建表，仅在表不存在时执行 CREATE TABLE，
确保幂等、安全、无 SQL 注入。

核心特性：
- 智能类型推断（JSON、布尔、数值、文本）
- 自动/显式主键识别
- 自动/显式索引建议
- 支持 PostgreSQL (JSONB) 和 MySQL (JSON)
- 安全转义表名/字段名
- 不提交事务（由调用方控制）

注意：不支持 SQLite（因 ALTER TABLE 限制严重，不适合动态建表）。
"""

import logging
import json
import hashlib
from typing import Any, Dict, Optional, Union, List

logger = logging.getLogger(__name__)

# 默认主键候选字段（大小写不敏感）
DEFAULT_PRIMARY_KEY_CANDIDATES = {"id", "pk", "uid", "uuid"}

# 默认索引候选字段（常用于查询条件）
DEFAULT_INDEX_CANDIDATES = {
    "user_id", "org_id", "tenant_id", "status", "type",
    "created_at", "updated_at", "deleted_at", "category"
}


def _is_valid_json_str(s: str) -> bool:
    """判断字符串是否为有效 JSON"""
    try:
        json.loads(s)
        return True
    except (ValueError, TypeError):
        return False


def _infer_column_type_postgresql(value: Any) -> str:
    if isinstance(value, bool):
        return "BOOLEAN"
    elif isinstance(value, int):
        return "BIGINT"
    elif isinstance(value, float):
        return "DOUBLE PRECISION"
    elif isinstance(value, str):
        if value.startswith(("{", "[")) and _is_valid_json_str(value):
            return "JSONB"
        else:
            return "TEXT"
    else:
        return "TEXT"


def _infer_column_type_mysql(value: Any) -> str:
    if isinstance(value, bool):
        return "TINYINT(1)"
    elif isinstance(value, int):
        return "BIGINT"
    elif isinstance(value, float):
        return "DOUBLE"
    elif isinstance(value, str):
        if value.startswith(("{", "[")) and _is_valid_json_str(value):
            return "JSON"
        else:
            return "TEXT"
    else:
        return "TEXT"


def _get_db_engine(conn) -> str:
    """从连接对象推断数据库类型"""
    # 尝试 Django 风格的 vendor 属性
    if hasattr(conn, 'vendor'):
        vendor = getattr(conn, 'vendor', '').lower()
        if vendor in ('postgresql', 'mysql'):
            return vendor

    # 通过模块名判断
    module_name = type(conn).__module__.split('.')[0]
    if module_name == 'psycopg2':
        return 'postgresql'
    elif module_name in ('pymysql', 'MySQLdb'):
        return 'mysql'
    elif module_name in ('sqlite3', 'pysqlite3'):
        raise ValueError("Dynamic table creation is not supported for SQLite due to DDL limitations.")
    else:
        raise ValueError(f"Unsupported database connection type: {type(conn)}")


def _quote_identifier(engine: str, name: str) -> str:
    """安全转义标识符（防 SQL 注入）"""
    if engine == "postgresql":
        return f'"{name}"'
    else:  # mysql
        return f"`{name}`"


def _build_column_definitions(
    engine: str,
    sample_data: Dict[str, Any],
    infer_func
) -> Dict[str, str]:
    """构建列定义字典：{col_name: "quoted_name TYPE"}"""
    col_defs = {}
    for key, value in sample_data.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"Invalid column name: {repr(key)}")
        col_name = key.strip()
        col_type = infer_func(value)
        quoted_name = _quote_identifier(engine, col_name)
        col_defs[col_name] = f"{quoted_name} {col_type}"
    return col_defs


def _detect_primary_key(
    table_name: str,
    columns: List[str],
    explicit_pk: Optional[Union[str, List[str]]] = None
) -> Optional[List[str]]:
    """确定主键字段列表"""
    if explicit_pk is not None:
        pk_list = [explicit_pk] if isinstance(explicit_pk, str) else list(explicit_pk)
        missing = set(pk_list) - set(columns)
        if missing:
            raise ValueError(f"Primary key field(s) not found in sample data: {missing}")
        return pk_list

    # 自动检测
    table_id = f"{table_name.rstrip('_')}_id".lower()
    candidates = [col for col in columns if col.lower() in DEFAULT_PRIMARY_KEY_CANDIDATES]
    if candidates:
        return [candidates[0]]
    if table_id in columns:
        return [table_id]
    return None


def _detect_indexes(
    columns: List[str],
    explicit_indexes: Optional[List[Union[str, List[str]]]] = None
) -> List[List[str]]:
    """合并显式与默认索引"""
    index_list: List[List[str]] = []

    # 显式索引
    if explicit_indexes:
        for idx in explicit_indexes:
            idx_cols = [idx] if isinstance(idx, str) else list(idx)
            missing = set(idx_cols) - set(columns)
            if missing:
                raise ValueError(f"Index field(s) not found: {missing}")
            index_list.append(idx_cols)

    # 默认单列索引（避免重复）
    existing_singles = {tuple(i) for i in index_list if len(i) == 1}
    for col in columns:
        if col.lower() in DEFAULT_INDEX_CANDIDATES and (col,) not in existing_singles:
            index_list.append([col])

    return index_list


def _generate_safe_index_name(table_name: str, cols: List[str]) -> str:
    """生成安全的索引名（防超长）"""
    base = f"idx_{table_name}_" + "_".join(cols)
    if len(base) <= 60:
        return base
    # 超长则哈希后缀
    suffix = hashlib.md5(base.encode()).hexdigest()[:8]
    prefix = table_name[:30]
    return f"idx_{prefix}_{suffix}"


def _create_table_with_constraints(
    conn,
    engine: str,
    table_name: str,
    col_defs: Dict[str, str],
    primary_key: Optional[List[str]] = None,
    indexes: Optional[List[List[str]]] = None
) -> bool:
    """执行建表及索引创建（不提交事务）"""
    quoted_table = _quote_identifier(engine, table_name)
    column_clauses = list(col_defs.values())

    # 添加主键约束
    if primary_key:
        pk_quoted = ", ".join(_quote_identifier(engine, col) for col in primary_key)
        column_clauses.append(f"PRIMARY KEY ({pk_quoted})")

    create_sql = f"CREATE TABLE IF NOT EXISTS {quoted_table} ({', '.join(column_clauses)});"

    with conn.cursor() as cur:
        cur.execute(create_sql)

    # 创建额外索引
    if indexes:
        for idx_cols in indexes:
            if primary_key and set(idx_cols) == set(primary_key):
                continue  # 主键已有索引
            idx_name = _generate_safe_index_name(table_name, idx_cols)
            idx_quoted_cols = ", ".join(_quote_identifier(engine, col) for col in idx_cols)
            idx_sql = (
                f"CREATE INDEX IF NOT EXISTS {_quote_identifier(engine, idx_name)} "
                f"ON {quoted_table} ({idx_quoted_cols});"
            )
            with conn.cursor() as cur:
                cur.execute(idx_sql)

    logger.info(f"[DDL] Ensured table exists: '{table_name}' on {engine.upper()}")
    return True


def ensure_table_exists(
    conn,
    table_name: str,
    sample_data: Dict[str, Any],
    *,
    primary_key: Optional[Union[str, List[str]]] = None,
    indexes: Optional[List[Union[str, List[str]]]] = None
) -> bool:
    """
    根据样例数据动态创建表（仅当不存在时）

    适用于低代码场景首次写入前自动建表。

    Args:
        conn: 数据库连接对象（支持 Django cursor.connection 或原生连接）
        table_name: 表名（将被安全转义）
        sample_data: 非空字典，用于推断字段类型
        primary_key: 显式指定主键字段（字符串或字段列表）
        indexes: 显式指定索引，如 ["user_id", ["status", "created_at"]]

    Returns:
        bool: 总是返回 True（因使用 IF NOT EXISTS，无法判断是否实际创建）

    Raises:
        ValueError: 输入无效或不支持的数据库
    """
    if not isinstance(sample_data, dict) or not sample_data:
        raise ValueError("sample_data must be a non-empty dict")
    if not isinstance(table_name, str) or not (table_name := table_name.strip()):
        raise ValueError("table_name must be a non-empty string")
    if any(c in table_name for c in (';', '--', '/*', '*/')):
        raise ValueError("table_name contains illegal characters")

    engine = _get_db_engine(conn)
    infer_func = _infer_column_type_postgresql if engine == "postgresql" else _infer_column_type_mysql
    col_defs = _build_column_definitions(engine, sample_data, infer_func)
    columns = list(col_defs.keys())

    pk = _detect_primary_key(table_name, columns, primary_key)
    idx_list = _detect_indexes(columns, indexes)

    return _create_table_with_constraints(
        conn=conn,
        engine=engine,
        table_name=table_name,
        col_defs=col_defs,
        primary_key=pk,
        indexes=idx_list
    )