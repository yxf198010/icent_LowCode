# lowcode/management/commands/batch_insert_data.py
# # 批量插入（无冲突处理）
# Json
# 编辑
# [
#   {"user_id": 1, "name": "Alice", "balance": 100.0},
#   {"user_id": 2, "name": "Bob", "balance": 200.0}
# ]
# python manage.py batch_insert_data \
#     --table=user_wallet \
#     --data-file=users.json
import json
import sys
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
import psycopg2
from typing import List, Dict, Any

class Command(BaseCommand):
    help = '批量插入数据到 PostgreSQL 表（不支持冲突处理，纯 INSERT）'

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
            help='JSON 文件路径，内容为字典列表，如 [{"col1": "val1"}, ...]'
        )
        parser.add_argument(
            '--db-alias',
            type=str,
            default='default',
            help='数据库别名（settings.DATABASES 中的 key，默认: default）'
        )

    def handle(self, *args, **options):
        table_name = options['table']
        data_file = options['data_file']
        db_alias = options['db_alias']

        if db_alias not in settings.DATABASES:
            raise CommandError(f"数据库别名 '{db_alias}' 未在 settings.DATABASES 中定义")

        db_config = settings.DATABASES[db_alias]
        if db_config['ENGINE'] != 'django.db.backends.postgresql':
            raise CommandError("当前仅支持 PostgreSQL 数据库")

        # 构建 psycopg2 连接参数
        conn_params = {
            'host': db_config.get('HOST', 'localhost'),
            'port': db_config.get('PORT', 5432),
            'dbname': db_config['NAME'],
            'user': db_config['USER'],
            'password': db_config['PASSWORD'],
            'sslmode': db_config.get('OPTIONS', {}).get('sslmode', 'prefer')
        }

        try:
            with open(data_file, 'r', encoding='utf-8') as f:
                data_list = json.load(f)
        except Exception as e:
            raise CommandError(f"读取数据文件失败: {e}")

        if not isinstance(data_list, list) or not data_list:
            raise CommandError("数据文件必须是非空的 JSON 列表")

        # 验证字段一致性
        first_keys = set(data_list[0].keys())
        for i, item in enumerate(data_list):
            if set(item.keys()) != first_keys:
                raise CommandError(f"第 {i+1} 条记录字段不一致，应与第一条相同")

        fields = list(first_keys)
        placeholders = ', '.join(['%s'] * len(fields))
        columns = ', '.join(fields)
        sql = f'INSERT INTO "{table_name}" ({columns}) VALUES ({placeholders})'

        try:
            with psycopg2.connect(**conn_params) as conn:
                with conn.cursor() as cursor:
                    values = [tuple(item[key] for key in fields) for item in data_list]
                    cursor.executemany(sql, values)
                    self.stdout.write(
                        self.style.SUCCESS(f"成功向表 '{table_name}' 批量插入 {len(values)} 条记录")
                    )
        except Exception as e:
            raise CommandError(f"批量插入失败: {e}")