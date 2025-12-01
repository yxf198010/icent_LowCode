# lowcode/management/commands/sync_tables.py
# # 正常同步
# python manage.py sync_tables
#
# # 仅预览
# python manage.py sync_tables --dry-run
#
# 开始同步动态模型表（dry-run: False）...
# ✔️ 表已存在: lowcode_user
# ✅ 成功创建表: lowcode_article
# ⚠️ 跳过无字段模型: EmptyModel
#
# 📊 同步完成！总计: 3 个模型 | 创建: 1 | 跳过: 2 | 失败: 0
from django.core.management.base import BaseCommand, CommandError
from lowcode.model_storage import load_all_model_configs
from lowcode.engine import get_dynamic_model_by_config
from lowcode.utils.db_utils import create_table_for_model, table_exists


class Command(BaseCommand):
    help = '为所有动态模型同步创建数据库表'

    def add_arguments(self, parser):
        parser.add_argument(
            '--dry-run',
            action='store_true',
            help='仅显示将要创建的表，不实际执行'
        )

    def handle(self, *args, **options):
        dry_run = options['dry_run']
        try:
            configs = load_all_model_configs()
        except Exception as e:
            raise CommandError(f"加载模型配置失败: {e}")

        if not configs:
            self.stdout.write(self.style.WARNING("⚠️ 未找到任何动态模型配置"))
            return

        created_count = 0
        skipped_count = 0
        failed_count = 0

        self.stdout.write(
            self.style.HTTP_INFO(f"开始同步动态模型表（dry-run: {dry_run}）...")
        )

        for model_name, config in configs.items():
            # 验证必要字段
            if not isinstance(config, dict):
                self.stdout.write(
                    self.style.ERROR(f"❌ 跳过无效配置: {model_name}（非字典类型）")
                )
                skipped_count += 1
                continue

            fields = config.get("fields")
            if not fields:
                self.stdout.write(
                    self.style.WARNING(f"⚠️ 跳过无字段模型: {model_name}")
                )
                skipped_count += 1
                continue

            table_name = config.get("table_name") or f"lowcode_{model_name.lower()}"

            if table_exists(table_name):
                self.stdout.write(
                    self.style.SUCCESS(f"✔️ 表已存在: {table_name}")
                )
                skipped_count += 1
                continue

            if dry_run:
                self.stdout.write(
                    self.style.MIGRATE_HEADING(f"[DRY-RUN] 将创建表: {table_name}")
                )
                created_count += 1  # 逻辑上“会创建”
                continue

            # 实际创建表
            try:
                DynamicModel = get_dynamic_model_by_config(
                    model_name=model_name,
                    fields=fields,
                    table_name=table_name
                )
                success = create_table_for_model(DynamicModel)
                if success:
                    self.stdout.write(
                        self.style.SUCCESS(f"✅ 成功创建表: {table_name}")
                    )
                    created_count += 1
                else:
                    self.stdout.write(
                        self.style.ERROR(f"❌ 创建表失败（无异常但未成功）: {table_name}")
                    )
                    failed_count += 1
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f"💥 创建表时出错: {table_name} | 错误: {e}")
                )
                failed_count += 1

        # 最终汇总
        total = len(configs)
        summary = (
            f"\n📊 同步完成！总计: {total} 个模型 | "
            f"创建: {created_count} | 跳过: {skipped_count} | 失败: {failed_count}"
        )

        if dry_run:
            self.stdout.write(self.style.HTTP_INFO(summary))
        elif failed_count == 0:
            self.stdout.write(self.style.SUCCESS(summary))
        else:
            self.stdout.write(self.style.WARNING(summary))
            raise CommandError("部分表创建失败，请检查日志")