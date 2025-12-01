# lowcode/management/commands/list_dynamic_methods.py
"""
管理命令：列出所有已绑定的动态方法（调试用）

Usage:
    python manage.py list_dynamic_methods [options]

Options:
    --model <name>     只显示指定模型的方法
    --type <type>      只显示指定类型的动态方法 (aggregate, field_update, custom_func)
    --active           只显示启用的配置
    --verbose          显示详细信息（包括参数）
"""
# 查看所有动态方法
# python manage.py list_dynamic_methods
#
# # 查看特定模型的方法
# python manage.py list_dynamic_methods --model DynamicOrder
#
# # 查看聚合类方法
# python manage.py list_dynamic_methods --type aggregate
#
# # 查看启用的配置
# python manage.py list_dynamic_methods --active
#
# # 查看详细信息（含参数）
# python manage.py list_dynamic_methods --verbose
#
# # 组合使用
# python manage.py list_dynamic_methods --model DynamicOrder --type field_update --active --verbose
# 📊 共找到 3 个动态方法配置：
#
# 📦 模型: DynamicOrder (DynamicOrder)
#    → calculate_total             | aggregate       | ✅ 启用
#    → update_status               | field_update    | ✅ 启用
#    → send_notification           | custom_func     | ❌ 已禁用
#
# 📦 模型: DynamicProduct (DynamicProduct)
#    → get_price_with_discount     | aggregate       | ✅ 启用
#
# ℹ️ 提示：可通过 `python manage.py reset_lowcode` 清除所有动态方法。
from django.core.management.base import BaseCommand, CommandError
from django.apps import apps
from django.db.models import Q

from lowcode.models.models import MethodLowCode
from lowcode.models.dynamic_model_factory import DYNAMIC_METHOD_PREFIX
import logging

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "列出所有已绑定的动态方法（调试用）"

    def add_arguments(self, parser):
        parser.add_argument(
            '--model',
            type=str,
            help='只显示指定模型的方法（如: DynamicOrder）'
        )
        parser.add_argument(
            '--type',
            type=str,
            choices=['aggregate', 'field_update', 'custom_func'],
            help='只显示指定类型的动态方法'
        )
        parser.add_argument(
            '--active',
            action='store_true',
            help='只显示启用的配置'
        )
        parser.add_argument(
            '--verbose',
            action='store_true',
            help='显示详细信息（包括参数）'
        )

    def handle(self, *args, **options):
        # 查询条件构建
        query = MethodLowCode.objects.all()

        if options['model']:
            query = query.filter(model_name=options['model'])
        if options['type']:
            query = query.filter(logic_type=options['type'])
        if options['active']:
            query = query.filter(is_active=True)

        # 获取所有符合条件的配置
        configs = query.select_related('model_name')  # 不需要 select_related，因为 model_name 是字符串

        if not configs.exists():
            self.stdout.write(
                self.style.WARNING("❌ 未找到匹配的动态方法配置。")
            )
            return

        # 按模型分组，便于输出
        from collections import defaultdict
        model_methods = defaultdict(list)

        for config in configs:
            model_methods[config.model_name].append({
                'method_name': config.method_name,
                'logic_type': config.logic_type,
                'params': config.params or {},
                'is_active': config.is_active,
            })

        # 输出结果
        self.stdout.write(self.style.SUCCESS(f"📊 共找到 {len(configs)} 个动态方法配置："))

        for model_name, methods in model_methods.items():
            try:
                dynamic_model = apps.get_model("lowcode", model_name)
                self.stdout.write(
                    self.style.HTTP_INFO(f"\n📦 模型: {model_name} ({dynamic_model.__name__})")
                )
            except Exception:
                self.stdout.write(
                    self.style.HTTP_INFO(f"\n📦 模型: {model_name} (类未注册)")
                )

            for method_info in methods:
                status = "✅ 启用" if method_info['is_active'] else "❌ 已禁用"
                method_name = method_info['method_name']
                logic_type = method_info['logic_type']

                line = f"   → {method_name:<25} | {logic_type:<15} | {status}"

                if options['verbose']:
                    params_str = str(method_info['params']).replace('\n', ', ')
                    line += f" | 参数: {params_str}"

                self.stdout.write(line)

        # 补充说明
        self.stdout.write("\nℹ️ 提示：可通过 `python manage.py reset_lowcode` 清除所有动态方法。")