# lowcode/management/commands/init_lowcode.py
"""
管理命令：手动初始化 LowCode 动态模型与方法。
适用于部署、测试或调试场景。
"""

from django.core.management.base import BaseCommand
from django.conf import settings
import logging

logger = logging.getLogger('lowcode')  # 使用 'lowcode' logger 保持一致


class Command(BaseCommand):
    help = "手动初始化 LowCode 动态模型与动态方法绑定"

    def add_arguments(self, parser):
        parser.add_argument(
            '--force',
            action='store_true',
            help='强制重新初始化（即使已初始化过）',
        )
        parser.add_argument(
            '--skip-methods',
            action='store_true',
            help='仅初始化动态模型，跳过方法绑定',
        )

    def handle(self, *args, **options):
        force = options['force']
        skip_methods = options['skip_methods']

        # 导入初始化函数（延迟导入避免循环依赖）
        try:
            from lowcode.dynamic_model_registry import initialize_dynamic_models
            from lowcode.models.dynamic_model_factory import bind_methods_from_db
        except ImportError as e:
            self.stderr.write(self.style.ERROR(f"❌ 导入初始化模块失败: {e}"))
            return

        # 检查是否已初始化（简单判断：可通过全局标志或自定义状态）
        # 这里我们不依赖 apps.py 中的 _DYNAMIC_INIT_DONE（因为管理命令是独立进程）
        # 所以每次运行都视为新会话，除非用户显式跳过

        self.stdout.write("🔄 开始初始化 LowCode 动态系统...")

        # 初始化动态模型
        try:
            self.stdout.write("📦 注册动态模型...")
            initialize_dynamic_models()
            self.stdout.write(self.style.SUCCESS("[OK] 动态模型注册完成"))
        except Exception as e:
            logger.exception("💥 动态模型初始化失败")
            self.stderr.write(self.style.ERROR(f"💥 动态模型初始化失败: {e}"))
            return

        # 绑定动态方法（除非跳过）
        if not skip_methods:
            try:
                self.stdout.write("🔗 绑定动态方法...")
                bind_methods_from_db()
                self.stdout.write(self.style.SUCCESS("[OK] 动态方法绑定完成"))
            except Exception as e:
                logger.exception("💥 动态方法绑定失败")
                self.stderr.write(self.style.ERROR(f"💥 动态方法绑定失败: {e}"))
                return

        self.stdout.write(
            self.style.SUCCESS("✅ LowCode 动态系统初始化成功！")
        )

        # 可选：提示用户下一步
        if not settings.DEBUG:
            self.stdout.write(
                self.style.WARNING(
                    "💡 提示：在生产环境中，建议在启动 Web 服务前运行此命令以预热系统。"
                )
            )