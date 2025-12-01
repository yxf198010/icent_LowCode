# lowcode/management/commands/list_models.py
# # 仅列出模型基本信息
# python manage.py list_models
#
# # 显示详细字段信息
# python manage.py list_models --verbose
# # 或
# python manage.py list_models -v
from django.core.management.base import BaseCommand, CommandError
from lowcode.model_storage import load_all_model_configs


class Command(BaseCommand):
    help = '列出所有已保存的动态模型'

    def add_arguments(self, parser):
        parser.add_argument(
            '--verbose', '-v',
            action='store_true',
            help='显示每个模型的详细字段信息'
        )

    def handle(self, *args, **options):
        try:
            configs = load_all_model_configs()
        except Exception as e:
            raise CommandError(f"加载模型配置时出错: {e}")

        if not configs:
            self.stdout.write(self.style.WARNING("未找到任何动态模型配置"))
            return

        verbose = options['verbose']
        total = len(configs)
        self.stdout.write(self.style.SUCCESS(f"✅ 找到 {total} 个动态模型：\n"))

        for model_name, config in configs.items():
            table_name = config.get("table_name", "未知")
            fields = config.get("fields", [])

            self.stdout.write(f"模型名称: {self.style.HTTP_INFO(model_name)}")
            self.stdout.write(f"  表名:     {table_name}")
            self.stdout.write(f"  字段数量: {len(fields)}")

            if verbose and fields:
                # 对齐字段输出
                max_name_len = max((len(field.get("name", "")) for field in fields), default=0)
                for field in fields:
                    name = field.get("name", "未知")
                    type_ = field.get("type", "未知")
                    padding = " " * (max_name_len - len(name) + 2)
                    self.stdout.write(f"    • {name}{padding}({type_})")
            elif verbose:
                self.stdout.write("    • (无字段)")

            self.stdout.write("")  # 空行分隔模型