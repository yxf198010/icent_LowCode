"""
Django 管理命令：动态创建数据库表

使用示例：
  python manage.py create_dynamic_table my_table \
    --sample '{"id":1,"user_id":100,"data":"{\\"key\\":\\"val\\"}","status":"active"}' \
    --primary-key id \
    --indexes user_id status \
    --indexes "user_id,status" \
    --database default

说明：
  - --sample 必填，JSON 格式
  - --primary-key 可选（默认自动检测）
  - --indexes 可多次指定，支持单列或逗号分隔的复合索引
  - --database 指定 Django DATABASES 中的别名（默认 'default'）
"""
# 使用示例
# 1. 基本用法（自动主键 + 自动索引）
# Bash
# 编辑
# python manage.py create_dynamic_table orders \
#   --sample '{"id":1,"user_id":100,"amount":99.9,"status":"paid"}'
# 自动：主键=id，索引=user_id, status
#
# 2. 显式主键 + 复合索引
# Bash
# 编辑
# python manage.py create_dynamic_table user_events \
#   --sample '{"event_id":"evt_123","user_id":100,"type":"click","ts":"2025-01-01"}' \
#   --primary-key event_id \
#   --indexes user_id \
#   --indexes "user_id,type"
# 3. 指定非默认数据库
# Bash
# 编辑
# python manage.py create_dynamic_table logs \
#   --sample '{"msg":"hello","level":"info"}' \
#   --database analytics_db
# 4. 无主键表（日志场景）
# Bash
# 编辑
# python manage.py create_dynamic_table raw_logs \
#   --sample '{"message":"test","timestamp":"2025-01-01"}' \
#   --primary-key ""  # 或省略 --primary-key
import json
import sys
from typing import List, Union
from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from lowcode.utils.table_manager import ensure_table_exists


class Command(BaseCommand):
    help = "动态创建数据库表（支持主键与索引）"

    def add_arguments(self, parser):
        parser.add_argument(
            "table_name",
            type=str,
            help="要创建的表名"
        )
        parser.add_argument(
            "--sample",
            type=str,
            required=True,
            help='样例数据（JSON 字符串），例如: \'{"id":1,"name":"test"}\''
        )
        parser.add_argument(
            "--primary-key",
            type=str,
            nargs="*",
            help="主键字段（可多个，用于复合主键）。若不指定则自动检测。"
        )
        parser.add_argument(
            "--indexes",
            action="append",
            default=[],
            help=(
                "索引字段（可多次使用）。支持两种格式：\n"
                "  --indexes user_id\n"
                "  --indexes user_id,status  （复合索引）"
            )
        )
        parser.add_argument(
            "--database",
            type=str,
            default="default",
            help="Django 数据库连接别名（来自 settings.DATABASES）"
        )

    def parse_indexes(self, raw_indexes: List[str]) -> List[Union[str, List[str]]]:
        """将命令行输入的索引参数解析为标准格式"""
        result = []
        for item in raw_indexes:
            if "," in item:
                result.append(item.split(","))
            else:
                result.append(item)
        return result

    def handle(self, *args, **options):
        table_name = options["table_name"]
        database = options["database"]

        # 解析 sample_data
        try:
            sample_data = json.loads(options["sample"])
        except json.JSONDecodeError as e:
            raise CommandError(f"--sample 不是有效的 JSON: {e}")

        if not isinstance(sample_data, dict) or not sample_data:
            raise CommandError("--sample 必须是非空 JSON 对象")

        # 解析主键
        primary_key = options["primary_key"]
        if primary_key is not None:
            if len(primary_key) == 1:
                primary_key = primary_key[0]  # 单字段字符串
            # 否则保留为列表（复合主键）

        # 解析索引
        indexes = self.parse_indexes(options["indexes"])

        # 获取数据库连接（Django 风格）
        if database not in connections:
            raise CommandError(f"数据库 '{database}' 未在 settings.DATABASES 中定义")

        conn = connections[database]

        self.stdout.write(
            self.style.NOTICE(
                f"准备在数据库 '{database}' 中创建表: {table_name}\n"
                f"  样例数据: {sample_data}\n"
                f"  主键: {primary_key or '（自动检测）'}\n"
                f"  索引: {indexes or '（自动检测）'}"
            )
        )

        try:
            created = ensure_table_exists(
                conn,
                table_name=table_name,
                sample_data=sample_data,
                primary_key=primary_key,
                indexes=indexes
            )
            if created:
                self.stdout.write(
                    self.style.SUCCESS(f"✅ 表 '{table_name}' 创建成功（或已存在）")
                )
            else:
                self.stdout.write(
                    self.style.WARNING(f"ℹ️  表 '{table_name}' 已存在，未执行创建")
                )
        except Exception as e:
            raise CommandError(f"创建表失败: {e}")