"""
Django 开发服务器重启命令（仅限本地开发使用）

用途：
  python manage.py restart_dev

注意：
  - 仅在 DEBUG=True 时可用
  - 会终止当前 runserver 进程并启动新实例
  - 新进程将在后台运行（Linux/macOS）或新窗口（Windows）
  - 请勿在生产环境或容器中使用
"""
# 运行命令
# Bash
# 编辑
# python manage.py restart_dev
# 当前终端会退出
# Linux/macOS：新 runserver 在后台运行（可通过 ps aux | grep runserver 查看）
# Windows：弹出新命令窗口运行服务器
# 仅当 DEBUG=True 时可用
# 不会自动检测服务就绪（需手动等待几秒）
# 不适用于 Docker / WSL2 / 远程开发（进程管理复杂）
# 不是热更新替代方案：真正的动态模型应在应用启动前生成
import os
import sys
import time
import logging
import subprocess
from django.core.management.base import BaseCommand
from django.conf import settings
from django.core.management import execute_from_command_line

try:
    import psutil
except ImportError:
    psutil = None

logger = logging.getLogger("django")


class Command(BaseCommand):
    help = "Restart Django development server (local use only)"

    def handle(self, *args, **options):
        if not settings.DEBUG:
            self.stderr.write(
                self.style.ERROR(
                    "❌ 错误：仅允许在 DEBUG=True 的开发环境中使用此命令！"
                )
            )
            sys.exit(1)

        if psutil is None:
            self.stderr.write(
                self.style.ERROR(
                    "❌ 错误：缺少依赖 'psutil'。请运行：pip install psutil"
                )
            )
            sys.exit(1)

        self.stdout.write(
            self.style.WARNING("⚠️  正在重启 Django 开发服务器...（仅限本地开发）")
        )

        # 查找当前项目的 runserver 进程 PID
        base_dir = os.path.abspath(settings.BASE_DIR)
        target_pid = None

        for proc in psutil.process_iter(["pid", "cmdline", "cwd"]):
            try:
                cmdline = proc.info.get("cmdline")
                cwd = proc.info.get("cwd")

                if not cmdline or not cwd:
                    continue

                if "manage.py" in cmdline and "runserver" in cmdline:
                    if os.path.abspath(cwd) == base_dir:
                        target_pid = proc.info["pid"]
                        break
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue

        # 终止旧进程
        if target_pid:
            self.stdout.write(f"  → 终止旧进程 (PID: {target_pid})...")
            try:
                p = psutil.Process(target_pid)
                p.terminate()
                p.wait(timeout=5)
            except psutil.TimeoutExpired:
                self.stdout.write("  ⚠️  进程未及时退出，强制杀死...")
                try:
                    p.kill()
                except psutil.NoSuchProcess:
                    pass
            except psutil.NoSuchProcess:
                pass

        # 启动新进程
        self.stdout.write("  → 启动新 Django 开发服务器...")
        python_exec = sys.executable
        cmd = [python_exec, "manage.py", "runserver"]

        try:
            if sys.platform == "win32":
                # Windows: 在新控制台窗口中启动（便于查看日志）
                subprocess.Popen(
                    cmd,
                    cwd=settings.BASE_DIR,
                    creationflags=subprocess.CREATE_NEW_CONSOLE
                )
            else:
                # Unix-like: 后台启动，日志仍输出到终端（不重定向）
                # 注意：父进程退出后，子进程可能被 init 接管
                subprocess.Popen(
                    cmd,
                    cwd=settings.BASE_DIR,
                    stdout=None,
                    stderr=None,
                    preexec_fn=os.setpgrp
                )
        except Exception as e:
            self.stderr.write(self.style.ERROR(f"  ❌ 启动失败: {e}"))
            sys.exit(1)

        self.stdout.write(
            self.style.SUCCESS(
                "✅ 新服务器已启动！\n"
                "   注意：当前终端即将退出，请在新窗口或后台查看日志。\n"
                "   如需停止，请手动 kill 新进程。"
            )
        )

        # 主动退出当前进程（避免残留）
        time.sleep(0.5)
        sys.exit(0)