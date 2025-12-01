# lowcode/management/commands/show_model.py
# # 查看模型详情（默认可读格式）
# python manage.py show_model User
#
# # 输出原始 JSON 配置（用于脚本或调试）
# python manage.py show_model Product --json
#
# 🔍 模型: Article
# 表名: lowcode_article
# 描述: 用户发布的文章
# 创建时间: 2025-10-01T10:00:00Z
#
# 字段配置:
# --------------------------------------------------
# 1. title (string)
#    显示名: 标题
#    参数:
#       {
#         "max_length": 200,
#         "blank": false
#       }
#
# 2. content (text)
#    显示名: 内容
#    参数:
#       {
#         "blank": true
#       }
import json
from django.core.management.base import BaseCommand, CommandError
from lowcode.model_storage import get_model_config


class Command(BaseCommand):
    help = '查看指定动态模型的详细配置'

    def add_arguments(self, parser):
        parser.add_argument('model_name', type=str, help='模型名称')
        parser.add_argument(
            '--json',
            action='store_true',
            help='以原始 JSON 格式输出完整配置（适合程序解析）'
        )

    def handle(self, *args, **options):
        model_name = options['model_name']
        config = get_model_config(model_name)

        if not config:
            raise CommandError(f"模型 '{model_name}' 不存在")

        if options['json']:
            # 直接输出原始配置（美化 JSON）
            self.stdout.write(json.dumps(config, ensure_ascii=False, indent=2))
            return

        # 可读性友好的格式化输出
        self.stdout.write(self.style.SUCCESS(f"🔍 模型: {model_name}"))

        # 基础信息
        table_name = config.get('table_name', '未知')
        description = config.get('description', '无')
        created_at = config.get('created_at', None)

        self.stdout.write(f"表名: {table_name}")
        self.stdout.write(f"描述: {description}")
        if created_at:
            self.stdout.write(f"创建时间: {created_at}")
        self.stdout.write("")

        # 字段列表
        fields = config.get("fields", [])
        if not fields:
            self.stdout.write(self.style.WARNING("⚠️  无字段定义"))
            return

        self.stdout.write("字段配置:")
        self.stdout.write("-" * 50)

        for i, field in enumerate(fields, 1):
            name = field.get("name", "未知")
            type_ = field.get("type", "未知")
            verbose_name = field.get("verbose_name", name)
            kwargs = field.get("kwargs", {})

            self.stdout.write(f"{i}. {self.style.HTTP_INFO(name)} ({type_})")
            self.stdout.write(f"   显示名: {verbose_name}")

            if kwargs:
                # 尝试美化 kwargs 输出
                try:
                    kwargs_str = json.dumps(kwargs, ensure_ascii=False, indent=2)
                    indented_kwargs = "\n".join("      " + line for line in kwargs_str.splitlines())
                    self.stdout.write(f"   参数:\n{indented_kwargs}")
                except Exception:
                    # 回退为普通字符串
                    self.stdout.write(f"   参数: {kwargs}")

            self.stdout.write("")  # 空行分隔字段