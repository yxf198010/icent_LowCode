# lowcode/management/commands/create_model.py
import json
import re
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from lowcode.model_storage import save_model_config, get_model_config
from lowcode.engine import get_dynamic_model_by_config
from lowcode.utils.db_utils import create_table_for_model, table_exists


def validate_model_name(name: str) -> str:
    """验证并标准化模型名称（仅允许字母、数字、下划线，且不以数字开头）"""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
        raise CommandError(
            "模型名称必须是有效的 Python 标识符（仅含字母、数字、下划线，且不以数字开头）"
        )
    return name


def generate_table_name(model_name: str) -> str:
    """生成安全的数据库表名"""
    # 转为小写并限制长度，替换非法字符（虽然 model_name 已校验，但双重保险）
    safe_name = re.sub(r'[^a-z0-9_]', '', model_name.lower())
    return f"lowcode_{safe_name}"[:63]  # PostgreSQL 限制 63 字节


class Command(BaseCommand):
    help = '创建新的动态模型并自动创建数据库表'

    def add_arguments(self, parser):
        parser.add_argument('model_name', type=str, help='模型名称（必须是有效 Python 标识符）')
        parser.add_argument('--fields', type=str, required=True, help='字段配置 JSON 字符串（数组格式）')
        parser.add_argument('--force', action='store_true', help='强制覆盖已存在的模型及数据库表')
        parser.add_argument('--no-db', action='store_true', help='仅保存模型配置，不创建数据库表')

    def handle(self, *args, **options):
        raw_model_name = options['model_name']
        fields_json = options['fields']
        force = options['force']
        no_db = options['no_db']

        # 1. 验证模型名
        model_name = validate_model_name(raw_model_name)

        # 2. 解析字段配置
        try:
            fields = json.loads(fields_json)
        except json.JSONDecodeError as e:
            raise CommandError(f"❌ 字段配置 JSON 格式错误: {e}")
        if not isinstance(fields, list):
            raise CommandError("❌ 字段配置必须是一个 JSON 数组")

        # 3. 检查是否已存在
        existing_config = get_model_config(model_name)
        if existing_config and not force:
            raise CommandError(
                f"❌ 模型 '{model_name}' 已存在。使用 --force 覆盖现有配置。"
            )

        # 4. 保存配置
        if not save_model_config(model_name, fields):
            raise CommandError("❌ 模型配置保存失败，请检查存储后端")

        self.stdout.write(self.style.SUCCESS(f"✅ 模型配置已保存: {model_name}"))

        # 5. 跳过数据库操作？
        if no_db:
            self.stdout.write(self.style.WARNING("⚠️ 跳过数据库表创建（--no-db）"))
            return

        # 6. 生成表名并检查表是否存在
        table_name = generate_table_name(model_name)

        if table_exists(table_name):
            if not force:
                self.stdout.write(
                    self.style.WARNING(
                        f"⚠️ 数据库表 '{table_name}' 已存在，且未使用 --force，跳过创建"
                    )
                )
                return
            else:
                self.stdout.write(
                    self.style.NOTICE(f"🔄 将覆盖已存在的表: {table_name}")
                )

        # 7. 动态构建模型类
        try:
            DynamicModel = get_dynamic_model_by_config(model_name, fields, table_name)
        except Exception as e:
            raise CommandError(f"❌ 构建动态模型失败: {e}")

        # 8. 创建数据库表
        try:
            # 注意：大多数数据库（如 MySQL）不支持 DDL 的事务回滚，
            # 所以 atomic() 对 CREATE TABLE 无实际回滚效果，但保留语义清晰
            with transaction.atomic():
                created = create_table_for_model(DynamicModel)
        except Exception as e:
            raise CommandError(f"❌ 创建数据库表时出错: {e}")

        if created:
            self.stdout.write(
                self.style.SUCCESS(f"✅ 数据库表已成功创建: {table_name}")
            )
        else:
            # 此情况可能因表已存在或权限不足等
            self.stdout.write(
                self.style.ERROR("❌ 数据库表创建失败（请查看日志详情）")
            )
            raise CommandError("数据库表创建失败")