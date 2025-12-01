# lowcode/management/commands/upsert_data.py
# Json
# [
#   {"user_id": 1, "name": "Alice", "balance": 100.0},
#   {"user_id": 2, "name": "Bob", "balance": 200.0}
# ]
# # UPSERT（基于 user_id 冲突）
# python manage.py upsert_data \
#     --table=user_wallet \
#     --data-file=users.json \
#     --conflict-field=user_id
# lowcode/management/commands/upsert_data.py
# lowcode/management/commands/upsert_data.py
import json
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from lowcode.utils.sql_template_wrapper import execute_upsert


class Command(BaseCommand):
    help = 'UPSERT 数据到 PostgreSQL 表（基于 SQLTemplate 封装，支持事务与自动回滚）'

    def add_arguments(self, parser):
        parser.add_argument(
            '--table',
            type=str,
            required=True,
            help='目标表名'
        )
        parser.add_argument(
            '--data-file',
            type=str,
            required=True,
            help='JSON 文件路径，内容为字典列表，如 [{"user_id":1, "name":"Alice"}, ...]'
        )
        parser.add_argument(
            '--conflict-field',
            type=str,
            required=True,
            help='冲突检测字段（必须是主键或唯一约束字段）'
        )
        parser.add_argument(
            '--db-alias',
            type=str,
            default='default',
            help='数据库别名（对应 settings.DATABASES 中的 key，默认: default）'
        )

    def handle(self, *args, **options):
        table = options['table']
        data_file = options['data-file']
        conflict_field = options['conflict-field']
        db_alias = options['db_alias']

        # 校验 db_alias
        if db_alias not in settings.DATABASES:
            raise CommandError(f"数据库别名 '{db_alias}' 未在 settings.DATABASES 中定义")

        # 读取 JSON 数据
        try:
            with open(data_file, 'r', encoding='utf-8') as f:
                data_list = json.load(f)
        except FileNotFoundError:
            raise CommandError(f"数据文件未找到: {data_file}")
        except json.JSONDecodeError as e:
            raise CommandError(f"JSON 格式错误: {e}")
        except Exception as e:
            raise CommandError(f"读取文件失败: {e}")

        if not isinstance(data_list, list):
            raise CommandError("数据文件内容必须是一个 JSON 列表")
        if not data_list:
            self.stdout.write(self.style.WARNING("数据为空，跳过操作"))
            return

        # 基础校验：每条记录是否包含 conflict_field
        for i, record in enumerate(data_list, start=1):
            if conflict_field not in record:
                raise CommandError(
                    f"第 {i} 条记录缺少冲突字段 '{conflict_field}'，记录内容: {record}"
                )

        # 调用封装好的 UPSERT 逻辑
        try:
            execute_upsert(
                table_name=table,
                data_list=data_list,
                conflict_field=conflict_field,
                db_alias=db_alias
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"✅ 成功 UPSERT {len(data_list)} 条记录到表 '{table}' "
                    f"(冲突字段: {conflict_field})"
                )
            )
        except Exception as e:
            raise CommandError(f"UPSERT 执行失败: {e}") from e