# lowcode/management/commands/reset_lowcode.py
"""
管理命令：重置 LowCode 动态系统状态。
用于清理动态模型、方法绑定及可选配置文件。
"""

import os
from pathlib import Path
from django.core.management.base import BaseCommand
from django.conf import settings
from django.apps import apps
import logging

logger = logging.getLogger('lowcode')


class Command(BaseCommand):
    help = "重置 LowCode 动态系统：清理模型、方法绑定及可选配置文件"

    def add_arguments(self, parser):
        parser.add_argument(
            '--delete-config',
            action='store_true',
            help='同时删除 dynamic_models.json 配置文件',
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help='跳过确认提示（用于自动化脚本）',
        )

    def handle(self, *args, **options):
        delete_config = options['delete_config']
        force = options['force']

        # 获取配置文件路径（需与你的项目一致）
        config_path = Path(settings.BASE_DIR) / 'dynamic_models.json'

        # 安全确认（除非 --force）
        if not force:
            self.stdout.write(
                self.style.WARNING("⚠️ 此操作将清除 LowCode 动态系统运行时状态。")
            )
            if delete_config:
                self.stdout.write(
                    self.style.ERROR(f"❗ 将永久删除配置文件: {config_path}")
                )
            confirm = input("继续? (y/N): ").strip().lower()
            if confirm not in ('y', 'yes'):
                self.stdout.write("❌ 操作已取消。")
                return

        self.stdout.write("🔄 开始重置 LowCode 动态系统...")

        # 1. 清理动态模型注册表（关键：清除内存中的模型类）
        try:
            from lowcode.dynamic_model_registry import cleanup_dynamic_models
            cleanup_dynamic_models()
            self.stdout.write(self.style.SUCCESS("[OK] 动态模型注册表已清理"))
        except ImportError:
            self.stdout.write(self.style.WARNING("⚠️ dynamic_model_registry.cleanup_dynamic_models 未实现，跳过模型清理"))
        except Exception as e:
            logger.exception("💥 清理动态模型失败")
            self.stderr.write(self.style.ERROR(f"💥 清理动态模型失败: {e}"))

        # 2. 清理动态方法绑定（如从模型类中移除注入的方法）
        try:
            from lowcode.models.dynamic_model_factory import cleanup_bound_methods
            cleanup_bound_methods()
            self.stdout.write(self.style.SUCCESS("[OK] 动态方法绑定已清理"))
        except ImportError:
            self.stdout.write(self.style.WARNING("⚠️ dynamic_model_method_bind.cleanup_bound_methods 未实现，跳过方法清理"))
        except Exception as e:
            logger.exception("💥 清理动态方法失败")
            self.stderr.write(self.style.ERROR(f"💥 清理动态方法失败: {e}"))

        # 3. （可选）删除配置文件
        if delete_config and config_path.exists():
            try:
                config_path.unlink()
                self.stdout.write(self.style.SUCCESS(f"[OK] 配置文件已删除: {config_path}"))
            except Exception as e:
                logger.exception("💥 删除配置文件失败")
                self.stderr.write(self.style.ERROR(f"💥 删除配置文件失败: {e}"))
        elif delete_config:
            self.stdout.write(self.style.WARNING(f"⚠️ 配置文件不存在: {config_path}"))

        # 4. 提示用户重启服务（如果正在运行）
        self.stdout.write(
            self.style.SUCCESS("✅ LowCode 动态系统重置完成！")
        )
        self.stdout.write(
            self.style.WARNING("💡 注意：若 Web 服务正在运行，请重启以确保状态完全清除。")
        )