# utils/django_utils.py
"""
Django 服务重启工具集
适用于开发/生产环境，常用于动态模型更新后热重启。
"""

import os
import sys
import time
import logging
import subprocess
from typing import Optional

import psutil
from django.conf import settings

logger = logging.getLogger(__name__)

# 防止重复重启（简单锁）
_RESTART_IN_PROGRESS = False


def run_command(cmd: str, cwd: Optional[str] = None) -> bool:
    """执行 shell 命令，返回是否成功"""
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            cwd=cwd or settings.BASE_DIR,
        )
        logger.info(f"✅ 命令执行成功: {cmd}")
        logger.debug(f"输出: {result.stdout[:500]}...")
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ 命令执行失败: {cmd}")
        logger.error(f"错误信息: {e.stderr}")
        return False


def find_runserver_pid() -> Optional[int]:
    """查找当前项目的 Django runserver 进程 PID（仅限开发环境）"""
    base_dir = os.path.abspath(settings.BASE_DIR)
    for proc in psutil.process_iter(["pid", "cmdline", "cwd"]):
        try:
            cmdline = proc.info.get("cmdline")
            cwd = proc.info.get("cwd")

            if not cmdline or not cwd:
                continue

            # 检查是否包含 manage.py runserver
            if "manage.py" in cmdline and "runserver" in cmdline:
                # 确保是当前项目目录下的进程
                if os.path.abspath(cwd) == base_dir:
                    return proc.info["pid"]
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue
    return None


def restart_django_dev_server():
    """重启开发环境的 Django runserver"""
    global _RESTART_IN_PROGRESS
    if _RESTART_IN_PROGRESS:
        logger.warning("🔄 重启已在进行中，跳过本次请求")
        return

    _RESTART_IN_PROGRESS = True
    try:
        pid = find_runserver_pid()
        if pid:
            logger.info(f"⚠️ 正在终止旧 runserver 进程 (PID: {pid})")
            try:
                p = psutil.Process(pid)
                p.terminate()
                p.wait(timeout=5)  # 等待最多 5 秒
            except psutil.TimeoutExpired:
                logger.warning("⏳ 进程未在 5 秒内退出，强制杀死")
                try:
                    p.kill()
                except psutil.NoSuchProcess:
                    pass
            except psutil.NoSuchProcess:
                pass

        # 启动新进程
        logger.info("🚀 正在启动新的 Django 开发服务器...")
        python_exec = sys.executable
        cmd = [python_exec, "manage.py", "runserver"]

        if sys.platform == "win32":
            # Windows: 隐藏新窗口（或根据需求显示）
            # 若希望显示窗口，去掉 creationflags
            creationflags = subprocess.CREATE_NEW_CONSOLE  # 弹出新窗口（更直观）
            subprocess.Popen(
                cmd,
                cwd=settings.BASE_DIR,
                creationflags=creationflags
            )
        else:
            # Unix-like: 后台运行
            subprocess.Popen(
                cmd,
                cwd=settings.BASE_DIR,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setpgrp  # 避免被父进程信号影响
            )

        logger.info("✅ Django 开发服务器已重启（请等待几秒加载）")
        # 注意：无法可靠检测服务是否就绪，建议前端轮询 /health/
    finally:
        _RESTART_IN_PROGRESS = False


def restart_django_prod_server():
    """
    重启生产环境服务。

    请在 settings.py 中配置：
        DJANGO_RESTART_COMMAND = "systemctl restart gunicorn"
    或
        DJANGO_RESTART_COMMAND = "docker restart my-django-app"
    """
    command = getattr(settings, "DJANGO_RESTART_COMMAND", None)
    if not command:
        logger.error(
            "❌ 未配置 DJANGO_RESTART_COMMAND，请在 settings.py 中设置生产环境重启命令。"
        )
        return False

    logger.info(f"🔧 执行生产环境重启命令: {command}")
    return run_command(command)


def restart_django_server():
    """
    统一重启入口：自动判断开发/生产环境

    使用示例（在管理命令或视图中）：
        from utils.django_utils import restart_django_server
        restart_django_server()
    """
    if settings.DEBUG:
        restart_django_dev_server()
    else:
        restart_django_prod_server()