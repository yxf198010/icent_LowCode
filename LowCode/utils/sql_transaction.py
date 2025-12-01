"""
通用 SQL 事务执行器（仅用于 DML：INSERT/UPDATE/DELETE/SELECT）

⚠️ 重要安全规则：
1. 所有用户输入必须通过 `params` 参数化（%s 占位符），禁止字符串拼接！
2. 动态表名/字段名必须使用 `quote_identifier()` 或 `connection.ops.quote_name()` 转义。
   示例：
      table = quote_identifier("user_table")
      sql = f"INSERT INTO {table} (name) VALUES (%s)"

🚫 不支持 DDL（CREATE/ALTER/DROP）混合事务（MySQL 会隐式提交）。
"""
# 使用示例（多数据库场景）
# Python
# 编辑
# from lowcode.utils.sql_transaction import execute_sql_transaction, quote_identifier
#
# def create_order_in_analytics_db(order_data):
#     table = quote_identifier("orders")
#     order_id = execute_sql_transaction(
#         [(f"INSERT INTO {table} (no, amount) VALUES (%s, %s)", (order_data["no"], order_data["amount"]))],
#         fetch_last_id=True,
#         database="analytics"  # ← 指定非 default 数据库
#     )
#     return order_id

# quote_identifier 默认使用 "default" 数据库的 quoting 规则（如 PostgreSQL 用双引号，MySQL 用反引号）。
# 若你在非 default 库中使用不同数据库类型（如 default=PostgreSQL, analytics=MySQL），应直接调用对应 connection 的 quote_name。
# 此工具仍不适用于 DDL（建表等），因为多数数据库（如 MySQL）会自动提交 DDL，破坏事务原子性。
import time
import logging
from typing import List, Tuple, Any, Optional
from django.db import connections
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

logger = logging.getLogger(__name__)

# 默认配置（可被 settings 覆盖）
DEFAULT_SQL_CONFIG = {
    "timeout": 10.0,
    "retry_times": 2,
    "retry_delay": 0.5,
    "allowed_exceptions": (
        "deadlock", "Deadlock", "could not serialize",
        "concurrent update", "lock timeout"
    )
}

SQL_CONFIG = {**DEFAULT_SQL_CONFIG, **getattr(settings, "UNIVERSAL_SQL_TRANSACTION_DEFAULTS", {})}


def _is_retryable_exception(e: Exception, allowed: tuple) -> bool:
    msg = str(e).lower()
    return any(kw.lower() in msg for kw in allowed)


def execute_sql_transaction(
    operations: List[Tuple[str, Tuple]],
    *,
    fetch_last_id: bool = False,
    timeout: Optional[float] = None,
    retry_times: Optional[int] = None,
    retry_delay: Optional[float] = None,
    allowed_exceptions: Optional[tuple] = None,
    database: str = "default"
):
    """
    在单个事务中执行一系列参数化 SQL 操作（仅 DML），支持重试、超时与 last_insert_id 获取。

    :param operations: 列表 of (sql_template, params_tuple)
                       - sql_template 必须使用 %s 占位符
                       - 动态标识符（表名/字段）必须提前用 quote_identifier() 转义
    :param fetch_last_id: 是否返回最后插入行的主键 ID（仅适用于单条 INSERT）
    :param timeout: 总超时时间（秒），从第一次尝试开始计时
    :param retry_times: 重试次数（默认从配置读取）
    :param retry_delay: 初始重试延迟（秒，指数退避）
    :param allowed_exceptions: 可重试的异常关键词列表
    :param database: Django 数据库连接别名（如 'default', 'analytics'）
    :return: 如果 fetch_last_id=True，返回 last_id；否则返回 None
    :raises: TimeoutError, DatabaseError, 或原始异常
    """
    if not operations:
        logger.warning("execute_sql_transaction called with empty operations list.")
        return None

    # 验证数据库别名是否存在
    if database not in connections:
        raise ImproperlyConfigured(f"Database alias '{database}' is not configured.")

    actual_timeout = timeout if timeout is not None else SQL_CONFIG["timeout"]
    actual_retry_times = retry_times if retry_times is not None else SQL_CONFIG["retry_times"]
    actual_retry_delay = retry_delay if retry_delay is not None else SQL_CONFIG["retry_delay"]
    actual_allowed = allowed_exceptions if allowed_exceptions is not None else SQL_CONFIG["allowed_exceptions"]

    last_exception = None
    start_time = time.time()

    for attempt in range(actual_retry_times + 1):
        try:
            conn = connections[database]
            with conn.cursor() as cursor:
                inner_start = time.time()
                for sql, params in operations:
                    if not isinstance(params, (tuple, list)):
                        raise ValueError("SQL parameters must be a tuple or list.")
                    cursor.execute(sql, params)

                last_id = None
                if fetch_last_id:
                    db_vendor = conn.vendor
                    if db_vendor == 'postgresql':
                        cursor.execute("SELECT LASTVAL();")
                    elif db_vendor == 'mysql':
                        cursor.execute("SELECT LAST_INSERT_ID();")
                    elif db_vendor == 'sqlite':
                        cursor.execute("SELECT last_insert_rowid();")
                    else:
                        raise NotImplementedError(f"Unsupported database vendor: {db_vendor}")
                    result = cursor.fetchone()
                    last_id = result[0] if result else None

                duration = time.time() - inner_start
                total_elapsed = time.time() - start_time
                if total_elapsed > actual_timeout:
                    raise TimeoutError(f"SQL transaction total time exceeded {actual_timeout}s")

                logger.info(
                    f"✅ SQL transaction succeeded | DB: {database} | Ops: {len(operations)} | "
                    f"Exec time: {duration:.3f}s | Attempts: {attempt + 1}"
                )
                return last_id if fetch_last_id else None

        except (TimeoutError, KeyboardInterrupt, SystemExit):
            raise

        except Exception as e:
            last_exception = e
            total_elapsed = time.time() - start_time
            if total_elapsed > actual_timeout:
                logger.warning("❌ Aborting retries due to total timeout.")
                break

            if attempt < actual_retry_times and _is_retryable_exception(e, actual_allowed):
                wait = actual_retry_delay * (2 ** attempt)
                logger.warning(
                    f"🔄 SQL transaction attempt {attempt + 1} failed (retryable): {e}, "
                    f"retrying in {wait:.2f}s..."
                )
                time.sleep(wait)
            else:
                break

    total_duration = time.time() - start_time
    logger.error(
        f"❌ SQL transaction failed after {actual_retry_times + 1} attempts | "
        f"DB: {database} | Ops: {len(operations)} | "
        f"Total time: {total_duration:.3f}s | Error: {last_exception}"
    )
    raise last_exception


def quote_identifier(name: str) -> str:
    """
    安全转义 SQL 标识符（表名、字段名等）。
    等价于: from django.db import connections; connections['default'].ops.quote_name(name)
    注意：此函数使用 'default' 数据库的 quoting 规则。
    如需指定数据库，请直接使用: connections[alias].ops.quote_name(name)
    """
    if not isinstance(name, str):
        raise TypeError("Identifier name must be a string.")
    # 使用 default 连接的 quoting 规则（大多数情况下足够）
    return connections["default"].ops.quote_name(name)