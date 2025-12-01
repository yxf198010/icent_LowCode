# lowcode/utils/db_operations.py

def table_exists(table_name: str, using: str = "default") -> bool:
    """
    检查指定数据库中是否存在某表（兼容 PostgreSQL / MySQL / SQLite）

    注意：table_name 会被转为小写（因部分数据库大小写敏感）
    """
    if not table_name:
        return False
    table = table_name.lower()

    from django.db import connections
    connection = connections[using]

    with connection.cursor() as cursor:
        vendor = connection.vendor
        if vendor == 'postgresql':
            cursor.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name = %s AND table_schema = CURRENT_SCHEMA()",
                [table]
            )
        elif vendor in ('mysql', 'mariadb'):
            cursor.execute(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name = %s",
                [table]
            )
        else:  # SQLite
            existing_tables = {t.lower() for t in connection.introspection.table_names()}
            return table in existing_tables
        return cursor.fetchone() is not None