# lowcode/core/raw_transaction.py
# 通用多数据库事务引擎,无 Django 依赖的通用 SQL 事务执行器，适用于任何 Python 项目。
"""
生产级原生 SQL 多表事务执行器（支持 PostgreSQL / MySQL）

职责：
- 管理数据库连接与事务生命周期
- 强制参数化查询（防 SQL 注入）
- 支持 Pydantic 参数校验（v1/v2 兼容）
- 结构化日志记录
- 自动回滚与资源清理

使用示例：
    class MyParams(BaseTransactionParams):
        user_id: int
        amount: float

    def logic(cursor, params):
        cursor.execute("UPDATE accounts SET balance = balance - %s WHERE id = %s",
                       (params['amount'], params['user_id']))

    tx = RawMultiTableTransaction({"engine": "postgresql", "host": "...", ...})
    tx.execute(logic, MyParams, user_id=123, amount=100.0)
"""
# 你的架构将更：
# 移除冗余注释，增强类型提示和文档
# 统一日志格式，对接项目全局 logger
# 保留 Pydantic 支持，但兼容 v1/v2
# ✅ 简洁（单一事务入口）
# ✅ 灵活（支持任意 SQL + 任意参数）
# ✅ 可维护（职责分离）
# ✅ 云原生友好（多数据库、无文件日志）
# lowcode/core/raw_transaction.py
"""
生产级原生 SQL 多表事务执行器（支持 PostgreSQL / MySQL）

职责：
- 管理数据库连接与事务生命周期
- 强制参数化查询（防 SQL 注入）
- 支持 Pydantic 参数校验（v1/v2 兼容）
- 使用字典游标（fetchone() 返回 dict）
- 结构化日志记录
- 自动回滚与资源清理

使用示例：
    class MyParams(BaseTransactionParams):
        user_id: int
        amount: float

    def logic(cursor, params):
        cursor.execute("UPDATE accounts SET balance = balance - %s WHERE id = %s",
                       (params['amount'], params['user_id']))

    tx = RawMultiTableTransaction({"engine": "postgresql", "host": "...", ...})
    tx.execute(logic, MyParams, user_id=123, amount=100.0)
"""

"""

通用原生多表事务工具，支持 MySQL 和 PostgreSQL。
适用于无法使用 Django ORM 的特殊场景（如动态表、跨库、遗留系统）。

⚠️ 使用须知：
- 必须使用参数化查询（%s），严禁 SQL 拼接
- 动态表名/字段名必须白名单校验
- 不支持 ORM 特性（信号、验证、缓存等）
- PostgreSQL 获取自增 ID 请使用 RETURNING 子句
"""
# def create_order(cursor, **kwargs):
#     user_id = kwargs["user_id"]
#     amount = kwargs["amount"]
#
#     # 兼容写法：使用 RETURNING（MySQL 8.0+ 支持；若用旧版 MySQL，可 fallback 到 lastrowid）
#     cursor.execute(
#         "INSERT INTO orders (user_id, amount) VALUES (%s, %s) RETURNING id",
#         (user_id, amount)
#     )
#     # PostgreSQL: 返回字典；MySQL: pymysql 不支持 RETURNING，会报错！

# 更稳妥方案：在业务逻辑中区分处理
# Python
# 编辑
# def create_order_and_update_stock(cursor, engine, **kwargs):
#     user_id = kwargs["user_id"]
#     goods_id = kwargs["goods_id"]
#     buy_num = kwargs["buy_num"]
#
#     if engine == "postgresql":
#         cursor.execute(
#             "INSERT INTO orders (user_id, goods_id, num) VALUES (%s, %s, %s) RETURNING id",
#             (user_id, goods_id, buy_num)
#         )
#         order_id = cursor.fetchone()["id"]
#     else:  # mysql
#         cursor.execute(
#             "INSERT INTO orders (user_id, goods_id, num) VALUES (%s, %s, %s)",
#             (user_id, goods_id, buy_num)
#         )
#         order_id = cursor.lastrowid  # 仅 MySQL
#
#     # 后续逻辑...

# tx = RawMultiTableTransaction(db_config)
# tx.execute(
#     business_logic=lambda cur, **kw: create_order_and_update_stock(cur, engine=tx.engine, **kw),
#     user_id=1001,
#     goods_id=5001,
#     buy_num=2
# )
# 使用示例（更新后）
# class OrderParams(BaseTransactionParams):
#     user_id: int
#     goods_id: int
#     buy_num: int
#
# def create_order_and_update_stock(cursor, params: dict):
#     # 插入订单
#     if tx.engine == "postgresql":
#         cursor.execute(
#             "INSERT INTO orders (user_id, goods_id, num) VALUES (%s, %s, %s) RETURNING id",
#             (params["user_id"], params["goods_id"], params["buy_num"])
#         )
#         order_id = cursor.fetchone()["id"]
#     else:  # mysql
#         cursor.execute(
#             "INSERT INTO orders (user_id, goods_id, num) VALUES (%s, %s, %s)",
#             (params["user_id"], params["goods_id"], params["buy_num"])
#         )
#         order_id = cursor.lastrowid
#
#     # 更新库存（示例）
#     cursor.execute(
#         "UPDATE inventory SET stock = stock - %s WHERE goods_id = %s",
#         (params["buy_num"], params["goods_id"])
#     )
#
#     print(f"创建订单 {order_id} 成功！")
#
# # 执行
# tx = RawMultiTableTransaction({
#     "engine": "postgresql",
#     "host": "localhost",
#     "user": "postgres",
#     "password": "xxx",
#     "database": "mydb"
# })
# tx.execute(create_order_and_update_stock, OrderParams, user_id=1001, goods_id=5001, buy_num=2)

# 假设你的 RawMultiTableTransaction 初始化如下：
#
# Python
# 编辑
# class RawMultiTableTransaction:
#     def __init__(self, db_config: dict):
#         self.host = db_config["host"]
#         self.port = db_config["port"]
#         self.database = db_config["database"]
#         self.user = db_config["user"]
#         self.password = db_config["password"]
#         self.vendor = db_config["vendor"]
#         # ... 建立连接
# 那么 get_db_config 返回的字典可直接传入：
#
# Python
# 编辑
# config = get_db_config("default")
# tx = RawMultiTableTransaction(config)
import logging
from contextlib import contextmanager
from typing import Callable, Dict, Any, Optional, Type, Union
from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)

# === 数据库驱动自动加载（带 DictCursor 支持）===
PG_AVAILABLE = False
MYSQL_AVAILABLE = False

try:
    import psycopg2
    from psycopg2 import OperationalError as PGOperationalError, DatabaseError as PGDatabaseError
    from psycopg2.extras import RealDictCursor as PGDictCursor

    PG_AVAILABLE = True
except ImportError:
    pass

try:
    import pymysql
    from pymysql import OperationalError as MySQLOperationalError, DatabaseError as MySQLDatabaseError
    from pymysql.cursors import DictCursor as MySQLDictCursor

    MYSQL_AVAILABLE = True
except ImportError:
    pass

# === 聚合数据库异常类型 ===
DB_ERRORS: tuple = ()
if PG_AVAILABLE:
    DB_ERRORS += (PGOperationalError, PGDatabaseError)
if MYSQL_AVAILABLE:
    DB_ERRORS += (MySQLOperationalError, MySQLDatabaseError)


class RawMultiTableTransaction:
    """
    通用原生 SQL 多表事务执行器（PostgreSQL / MySQL）
    所有 fetch 操作返回字典（字段名 -> 值）
    """

    def __init__(self, db_config: Dict[str, Any]):
        """
        初始化事务执行器

        :param db_config: 数据库配置字典，必须包含 'engine' 字段
                          engine 取值: "postgresql" 或 "mysql"
                          其他字段将直接传递给数据库驱动（如 host, port, user, password, database/dbname 等）
        :raises ValueError: engine 无效
        :raises RuntimeError: 对应数据库驱动未安装
        """
        if not isinstance(db_config, dict):
            raise TypeError("db_config 必须是字典")

        self.db_config = db_config.copy()
        self.engine = self.db_config.pop("engine", None)

        if self.engine not in ("postgresql", "mysql"):
            raise ValueError('db_config 必须包含 engine="postgresql" 或 "mysql"')

        if self.engine == "postgresql" and not PG_AVAILABLE:
            raise RuntimeError("psycopg2 未安装，请运行: pip install psycopg2-binary")
        if self.engine == "mysql" and not MYSQL_AVAILABLE:
            raise RuntimeError("pymysql 未安装，请运行: pip install pymysql")

    @contextmanager
    def _transaction(self):
        """管理数据库连接、事务、游标和资源释放（使用 DictCursor）"""
        conn = None
        cursor = None
        try:
            # 建立连接（启用字典游标）
            if self.engine == "postgresql":
                conn = psycopg2.connect(cursor_factory=PGDictCursor, **self.db_config)
            elif self.engine == "mysql":
                conn = pymysql.connect(cursorclass=MySQLDictCursor, **self.db_config)

            cursor = conn.cursor()
            logger.debug(f"[{self.engine}] 数据库连接成功，开启事务")

            yield cursor

            # 提交事务
            conn.commit()
            logger.info(f"[{self.engine}] 事务执行成功，已提交")

        except DB_ERRORS as e:
            if conn:
                conn.rollback()
            logger.error(f"[{self.engine}] 数据库操作异常，已回滚: {e}", exc_info=True)
            raise
        except Exception as e:
            if conn:
                conn.rollback()
            logger.error(f"[{self.engine}] 业务逻辑异常，已回滚: {e}", exc_info=True)
            raise
        finally:
            if cursor:
                cursor.close()
                logger.debug(f"[{self.engine}] 游标已关闭")
            if conn:
                conn.close()
                logger.debug(f"[{self.engine}] 数据库连接已关闭")

    def _validate_params(
        self,
        params: Dict[str, Any],
        validator: Optional[Type[BaseModel]]
    ) -> Dict[str, Any]:
        """使用 Pydantic 校验参数（兼容 v1 和 v2）"""
        if validator is None:
            return params

        try:
            # Pydantic v2
            if hasattr(validator, 'model_validate'):
                validated = validator.model_validate(params)
                return validated.model_dump()
            # Pydantic v1
            else:
                validated = validator(**params)
                return validated.dict()
        except ValidationError as e:
            error_msg = f"参数校验失败: {e.errors()}"
            logger.error(error_msg)
            raise ValueError(error_msg) from e

    def execute(
        self,
        business_logic: Callable[[Any, Dict[str, Any]], None],
        params_validator: Optional[Type[BaseModel]] = None,
        **kwargs
    ) -> None:
        """
        执行带事务的业务逻辑

        :param business_logic: 函数签名必须为 (cursor, validated_params: dict) -> None
                               cursor.fetch*() 返回字典（字段名 -> 值）
        :param params_validator: 可选的 Pydantic 模型类，用于校验 kwargs
        :param kwargs: 动态传入的业务参数
        :raises ValueError: 参数校验失败
        :raises Exception: 业务逻辑或数据库异常（已自动回滚）
        """
        validated_params = self._validate_params(kwargs, params_validator)

        with self._transaction() as cursor:
            business_logic(cursor, validated_params)


class BaseTransactionParams(BaseModel):
    """
    基础事务参数模型，允许额外字段（适合动态场景）

    示例：
        class OrderParams(BaseTransactionParams):
            order_id: int
            items: list[str]
    """

    class Config:
        extra = "allow"  # 允许传入未声明字段（如 trace_id, remark 等）