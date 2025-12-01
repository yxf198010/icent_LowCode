# lowcode/management/commands/update_model.py
# # 更新模型字段（自动 ALTER TABLE）
# python manage.py update_model Article \
#   --fields '[{"name": "title", "type": "string", "kwargs": {"max_length": 200}}, {"name": "is_published", "type": "boolean"}]'
#
# # 仅更新配置，不碰数据库
# python manage.py update_model User --fields '[...]' --no-db
#
# # 表不存在时强制创建
# python manage.py update_model Log --fields '[...]' --force
import json
from django.core.management.base import BaseCommand, CommandError
from lowcode.model_storage import get_model_config, save_model_config
from lowcode.engine import get_dynamic_model_by_config
from lowcode.utils.db_utils import (
    alter_table_for_model,
    table_exists,
    create_table_for_model
)


class Command(BaseCommand):
    help = '更新动态模型结构（支持字段增删改）并自动 ALTER TABLE'

    def add_arguments(self, parser):
        parser.add_argument('model_name', type=str, help='模型名称')
        parser.add_argument(
            '--fields',
            type=str,
            required=True,
            help='新的完整字段配置 JSON（格式: [{"name": "title", "type": "string", ...}, ...]'
        )
        parser.add_argument(
            '--no-db',
            action='store_true',
            help='仅更新配置，不修改数据库结构'
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='若表不存在，则强制创建（而非报错）'
        )

    def handle(self, *args, **options):
        model_name = options['model_name']
        fields_json = options['fields']
        no_db = options['no_db']
        force = options['force']

        # === 1. 解析并验证新字段配置 ===
        try:
            new_fields = json.loads(fields_json)
        except json.JSONDecodeError as e:
            raise CommandError(f"❌ 字段配置不是有效的 JSON: {e}")

        if not isinstance(new_fields, list):
            raise CommandError("❌ 字段配置必须是一个 JSON 数组（列表）")

        # 基础字段结构校验（可选但推荐）
        for i, field in enumerate(new_fields):
            if not isinstance(field, dict):
                raise CommandError(f"❌ 第 {i+1} 个字段不是对象")
            if 'name' not in field or 'type' not in field:
                raise CommandError(f"❌ 第 {i+1} 个字段缺少 'name' 或 'type'")

        # === 2. 获取旧配置 ===
        old_config = get_model_config(model_name)
        if not old_config:
            raise CommandError(f"❌ 模型 '{model_name}' 不存在。请先使用 create_model 创建。")

        old_fields = old_config.get("fields", [])
        table_name = old_config.get("table_name") or f"lowcode_{model_name.lower()}"

        # === 3. 保存新配置 ===
        if not save_model_config(model_name, new_fields):
            raise CommandError("❌ 模型配置保存失败")

        self.stdout.write(self.style.SUCCESS(f"✅ 模型配置已更新: {model_name}"))

        if no_db:
            self.stdout.write(self.style.WARNING("⚠️ 跳过数据库变更（--no-db 模式）"))
            return

        # === 4. 处理数据库表 ===
        if not table_exists(table_name):
            if force:
                self.stdout.write(
                    self.style.WARNING(
                        f"⚠️ 表 '{table_name}' 不存在，但 --force 已启用，将尝试创建新表..."
                    )
                )
                DynamicModel = get_dynamic_model_by_config(model_name, new_fields, table_name)
                if create_table_for_model(DynamicModel):
                    self.stdout.write(self.style.SUCCESS(f"✅ 表已成功创建: {table_name}"))
                else:
                    raise CommandError("❌ 表创建失败，请检查数据库权限或字段定义")
                return
            else:
                raise CommandError(
                    f"❌ 数据库表 '{table_name}' 不存在。如需自动创建，请添加 --force 参数。"
                )

        # === 5. 执行结构变更（ALTER TABLE）===
        DynamicModel = get_dynamic_model_by_config(model_name, new_fields, table_name)

        try:
            success = alter_table_for_model(DynamicModel, old_fields, new_fields)
        except Exception as e:
            raise CommandError(f"💥 执行 ALTER TABLE 时发生异常: {e}")

        if success:
            self.stdout.write(self.style.SUCCESS(f"✅ 数据库表结构已更新: {table_name}"))
            self.stdout.write(
                self.style.WARNING(
                    "\n💡 注意：字段删除或类型变更可能导致数据丢失！\n"
                    "建议使用以下命令迁移历史数据：\n"
                    "  python manage.py migrate_data {model_name} --copy-field ...\n"
                    "  python manage.py migrate_data {model_name} --default-value ...\n".format(
                        model_name=model_name
                    )
                )
            )
        else:
            raise CommandError(
                "❌ 数据库表结构更新失败（alter_table_for_model 返回 False）。\n"
                "请检查字段兼容性、数据库权限及日志输出。"
            )