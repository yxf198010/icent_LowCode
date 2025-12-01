# lowcode/management/commands/migrate_data.py
# # 示例 1：重命名字段（自动适配数据库）
# python manage.py migrate_data User --rename-field username user_name
#
# # 示例 2：复制字段
# python manage.py migrate_data Product --copy-field name title
#
# # 示例 3：设置默认值（支持 JSON）
# python manage.py migrate_data Order --default-value is_paid false
# python manage.py migrate_data Article --default-value tags '["news"]'
#
# # 示例 4：执行自定义 SQL
# python manage.py migrate_data Log --sql "UPDATE lowcode_log SET level='INFO' WHERE level=''"
import json
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.backends.utils import names_digest
from lowcode.model_storage import get_model_config


class Command(BaseCommand):
    help = '在字段变更前后迁移数据（例如：重命名字段、转换类型）'

    def add_arguments(self, parser):
        parser.add_argument('model_name', type=str, help='模型名称')
        parser.add_argument('--sql', type=str, help='执行自定义 SQL（推荐用于复杂迁移）')
        parser.add_argument('--rename-field', nargs=2, metavar=('OLD', 'NEW'), help='重命名字段（自动适配数据库）')
        parser.add_argument('--copy-field', nargs=2, metavar=('SRC', 'DST'), help='将 SRC 字段值复制到 DST（仅当 DST 为 NULL 时）')
        parser.add_argument('--default-value', nargs=2, metavar=('FIELD', 'VALUE'), help='为 FIELD 设置默认值（JSON 或字符串）')

    def handle(self, *args, **options):
        model_name = options['model_name']
        config = get_model_config(model_name)
        if not config:
            raise CommandError(f"模型 '{model_name}' 不存在")

        table_name = config.get("table_name", f"lowcode_{model_name.lower()}")

        # 获取当前表的所有字段名（用于校验）
        existing_columns = self._get_table_columns(table_name)

        # 所有写操作必须在事务中进行
        with transaction.atomic():
            if options['sql']:
                self._execute_custom_sql(table_name, options['sql'])

            elif options['rename_field']:
                old, new = options['rename_field']
                self._rename_column(table_name, old, new, existing_columns)

            elif options['copy_field']:
                src, dst = options['copy_field']
                self._copy_field_value(table_name, src, dst, existing_columns)

            elif options['default_value']:
                field, value_str = options['default_value']
                self._set_default_value(table_name, field, value_str, existing_columns)

            else:
                self.stdout.write(
                    self.style.WARNING(
                        "⚠️ 未指定操作。常用示例：\n"
                        "  --sql \"UPDATE lowcode_user SET status='active' WHERE status_old='1'\"\n"
                        "  --rename-field old_name new_name\n"
                        "  --copy-field old_name new_name\n"
                        "  --default-value is_active true"
                    )
                )

    def _get_table_columns(self, table_name):
        """获取指定表的所有列名（不区分大小写，返回小写集合）"""
        with connection.cursor() as cursor:
            cursor.execute(f"PRAGMA table_info({table_name})") if connection.vendor == 'sqlite' \
                else cursor.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = DATABASE() AND table_name = %s
                """, [table_name]) if connection.vendor == 'mysql' \
                else cursor.execute("""
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = %s
                """, [table_name])

            if connection.vendor == 'sqlite':
                columns = {row[1].lower() for row in cursor.fetchall()}
            else:
                columns = {row[0].lower() for row in cursor.fetchall()}
        return columns

    def _ensure_columns_exist(self, table_name, *column_names, existing_columns=None):
        """确保指定字段存在，否则抛出错误"""
        if existing_columns is None:
            existing_columns = self._get_table_columns(table_name)
        missing = [col for col in column_names if col.lower() not in existing_columns]
        if missing:
            raise CommandError(f"表 '{table_name}' 中缺少字段: {', '.join(missing)}")

    def _execute_custom_sql(self, table_name, sql):
        self.stdout.write(f"执行自定义 SQL: {sql}")
        with connection.cursor() as cursor:
            cursor.execute(sql)
        self.stdout.write(self.style.SUCCESS("✅ 自定义 SQL 执行成功"))

    def _copy_field_value(self, table_name, src, dst, existing_columns):
        self._ensure_columns_exist(table_name, src, dst, existing_columns=existing_columns)
        with connection.cursor() as cursor:
            # 使用参数化避免注入（但字段名不能参数化，需校验）
            cursor.execute(f'UPDATE "{table_name}" SET "{dst}" = "{src}" WHERE "{dst}" IS NULL')
            rows = cursor.rowcount
        self.stdout.write(self.style.SUCCESS(f"✅ 已复制 {rows} 行数据: {src} → {dst}"))

    def _set_default_value(self, table_name, field, value_str, existing_columns):
        self._ensure_columns_exist(table_name, field, existing_columns=existing_columns)

        # 安全地解析并转义值
        try:
            parsed = json.loads(value_str)
        except json.JSONDecodeError:
            parsed = value_str  # 保持为字符串

        # 使用 connection.ops.adapt_unknown_value 或 quote_value（Django 内部方法）
        # 更安全的方式：构造带参数的 UPDATE（但字段名仍需手动处理）
        with connection.cursor() as cursor:
            # 注意：字段名已校验，此处可安全插入
            sql = f'UPDATE "{table_name}" SET "{field}" = %s WHERE "{field}" IS NULL'
            cursor.execute(sql, [parsed])
            rows = cursor.rowcount
        self.stdout.write(self.style.SUCCESS(f"✅ 已填充 {rows} 行默认值: {field} = {value_str}"))

    def _rename_column(self, table_name, old, new, existing_columns):
        self._ensure_columns_exist(table_name, old, existing_columns=existing_columns)
        if new.lower() in existing_columns:
            raise CommandError(f"目标字段 '{new}' 已存在，无法重命名")

        vendor = connection.vendor
        with connection.cursor() as cursor:
            try:
                if vendor in ('postgresql', 'mysql') and vendor != 'sqlite':
                    # PostgreSQL 和 MySQL 8.0+ 支持 RENAME COLUMN
                    cursor.execute(f'ALTER TABLE "{table_name}" RENAME COLUMN "{old}" TO "{new}"')
                    self.stdout.write(self.style.SUCCESS(f"✅ 字段已重命名: {old} → {new}"))
                elif vendor == 'sqlite':
                    # SQLite 不支持 RENAME COLUMN（旧版本），采用重建表方式
                    self._rename_column_sqlite(cursor, table_name, old, new)
                else:
                    raise CommandError(f"不支持的数据库后端: {vendor}")
            except Exception as e:
                raise CommandError(f"重命名字段失败: {e}")

    def _rename_column_sqlite(self, cursor, table_name, old, new):
        """SQLite 专用：通过重命名表 + 创建新表 + 复制数据 + 删除旧表实现字段重命名"""
        # 1. 获取原表结构
        cursor.execute(f"PRAGMA table_info({table_name})")
        columns = cursor.fetchall()  # (cid, name, type, notnull, dflt_value, pk)

        # 构建新列定义
        new_columns = []
        for _, name, col_type, notnull, default_val, pk in columns:
            col_name = new if name == old else name
            col_def = f'"{col_name}" {col_type}'
            if pk:
                col_def += " PRIMARY KEY"
            if notnull:
                col_def += " NOT NULL"
            if default_val is not None:
                col_def += f" DEFAULT {default_val}"
            new_columns.append(col_def)

        new_table_name = f"{table_name}_renamed_{names_digest(old, new)[:8]}"

        # 2. 创建新表
        create_sql = f'CREATE TABLE "{new_table_name}" ({", ".join(new_columns)})'
        cursor.execute(create_sql)

        # 3. 复制数据（字段映射）
        old_names = [col[1] for col in columns]
        new_names = [new if n == old else n for n in old_names]
        cursor.execute(
            f'INSERT INTO "{new_table_name}" ({", ".join(f"""\"{n}\"""" for n in new_names)}) '
            f'SELECT {", ".join(f"""\"{o}\"""" for o in old_names)} FROM "{table_name}"'
        )

        # 4. 删除旧表，重命名新表
        cursor.execute(f'DROP TABLE "{table_name}"')
        cursor.execute(f'ALTER TABLE "{new_table_name}" RENAME TO "{table_name}"')

        self.stdout.write(self.style.SUCCESS(f"✅ SQLite 字段已重命名: {old} → {new}"))