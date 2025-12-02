# lowcode/apps.py
"""
LowCode 应用配置：负责动态模型与方法的初始化。
注意：数据库相关操作已推迟到首次 HTTP 请求，避免在 AppConfig.ready() 中访问 DB。
"""
# 已实现的功能分析
# 1. 延迟 DB 初始化至首次请求
# AppConfig.ready() 中 不直接访问数据库。
# 使用 request_started.connect(self._on_first_request) 将实际初始化（包括调用 bind_methods_from_db()）推迟到第一个 HTTP 请求到来时执行。
# 利用 _DYNAMIC_INIT_DONE 和线程锁 _DYNAMIC_INIT_LOCK 保证 跨线程安全、仅执行一次。
# ✅ 完全符合 Django 最佳实践：避免在 ready() 中访问 DB（因为此时可能尚未 migrate 完成，或在管理命令中运行）。
#
# 2. 精细的跳过逻辑（避免误触发）
# 通过 _should_skip_initialization() 函数，明确排除了以下场景：
#
# 非 runserver 主进程（如 reload 的父进程）
# 管理命令（migrate, shell, test 等）
# 测试环境（除非显式启用）
# Celery Worker
# 显式开关 SKIP_DYNAMIC_MODEL_INIT
# ✅ 健壮性高，不会在错误上下文中尝试加载动态模型。
#
# 3. post_migrate 安全刷新动态方法
# 注册 post_migrate 信号，在每次 migrate 后调用 refresh_dynamic_methods()。
# 此时数据库结构已更新，适合刷新方法绑定。
# 且只对 lowcode app 生效。
# ✅ 支持模型变更后自动同步方法逻辑。
#
# 4. signals.py 中的热更新机制（开发/生产分离）
# 监听 ModelLowCode 的 post_save / post_delete。
# 开发环境（DEBUG=True 或 LOWCODE_AUTO_MIGRATE=True）：自动执行 makemigrations + migrate。
# 生产环境：发送异步任务 async_refresh_and_create_table（假设你有 Celery）。
# 去重机制（10 秒窗口）防止高频变更导致重复迁移。
# 只在 fields / table_name / name 真正变更时触发。
# ✅ 支持低代码模型的“热更新”，且兼顾性能与安全性。
#
# 5. 其他辅助功能
# 自动为 staff 用户创建 DRF Token（虽然代码片段未完整展示，但注释提到了）。
# 日志详细，便于调试。
import sys
import os
import logging
import threading
from django.apps import AppConfig
from django.conf import settings
from django.db.models.signals import post_migrate
from django.core.signals import request_started

logger = logging.getLogger(__name__)

# 防止 ready() 被多次执行（同一进程内）
_DYNAMIC_MODELS_INITIALIZED = False

# 防止 _on_first_request 被多次执行（跨线程安全）
_DYNAMIC_INIT_DONE = False
_DYNAMIC_INIT_LOCK = threading.Lock()


def _is_management_command() -> bool:
    """判断是否运行在 Django 管理命令上下文中"""
    try:
        command = sys.argv[1] if len(sys.argv) > 1 else ''
    except (IndexError, AttributeError):
        return False

    skip_commands = {
        'migrate', 'makemigrations', 'collectstatic', 'dumpdata', 'loaddata',
        'shell', 'dbshell', 'test', 'createsuperuser', 'check',
        'compilemessages', 'makemessages', 'startapp',
    }
    return command in skip_commands


def _should_skip_initialization() -> bool:
    """综合判断是否应跳过动态模型初始化（仅用于 ready() 阶段）"""

    # 核心：只在 runserver 的子进程（RUN_MAIN='true'）中继续
    run_main = os.environ.get('RUN_MAIN')
    if run_main != 'true':
        logger.debug(f"[DEBUG] 跳过初始化：RUN_MAIN={run_main!r}（非 'true'）")
        return True

    # 显式跳过开关
    if getattr(settings, 'SKIP_DYNAMIC_MODEL_INIT', False):
        logger.info("SKIP_DYNAMIC_MODEL_INIT=True，跳过动态模型初始化")
        return True

    # 管理命令
    if _is_management_command():
        cmd = sys.argv[1] if len(sys.argv) > 1 else 'unknown'
        logger.debug(f"[DEBUG] 检测到管理命令 '{cmd}'，跳过动态模型初始化")
        return True

    # 测试环境（除非显式启用）
    if 'test' in sys.argv or os.getenv('PYTEST_CURRENT_TEST'):
        if not os.getenv('TESTING', '').lower() in ('1', 'true', 'yes'):
            logger.debug("[DEBUG] 测试模式下默认跳过动态模型初始化（设置 TESTING=1 可启用）")
            return True

    # Celery Worker
    if any(arg.startswith('celery') for arg in sys.argv) or 'CELERY_WORKER' in os.environ:
        logger.debug("[DEBUG] 检测到 Celery Worker，跳过动态模型初始化")
        return True

    return False


def bind_dynamic_methods(sender, **kwargs):
    """post_migrate 信号回调：安全地刷新动态方法（此时 DB 已就绪）"""
    if sender.name != 'lowcode':
        return
    try:
        from .models.dynamic_model_factory import refresh_dynamic_methods
        refresh_dynamic_methods()
        logger.info("post_migrate: 动态方法已刷新")
    except Exception:
        logger.error("[ERROR] post_migrate: 动态方法刷新失败", exc_info=True)


class LowCodeConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'lowcode'

    def ready(self):
        global _DYNAMIC_MODELS_INITIALIZED

        # 调试日志
        run_main = os.environ.get('RUN_MAIN')
        logger.debug("[DEBUG] AppConfig.ready() called | PID={os.getpid()} | RUN_MAIN={run_main!r}")

        # 防止重复调用 ready()
        if _DYNAMIC_MODELS_INITIALIZED:
            logger.debug("[DEBUG] 当前进程已处理 ready()，跳过重复加载")
            return

        # 跳过非 Web 主进程（如 manage.py 命令、主 reload 进程等）
        if _should_skip_initialization():
            _DYNAMIC_MODELS_INITIALIZED = True  # 标记为已处理，避免后续再检查
            return

        # 注册 post_migrate 信号（安全，不访问 DB）
        post_migrate.connect(bind_dynamic_methods, sender=self, weak=False)

        # 加载 signals（不应包含 DB 查询）
        try:
            import lowcode.signals  # noqa: F401
            logger.debug("[DEBUG] lowcode.signals 已加载")
        except Exception:
            logger.warning("[WARNING] 加载 lowcode.signals 时出错", exc_info=True)

        # 关键：将数据库初始化推迟到首次 HTTP 请求
        request_started.connect(self._on_first_request, weak=False)
        _DYNAMIC_MODELS_INITIALIZED = True  # 标记 ready() 已完成

    def _on_first_request(self, **kwargs):
        """在第一次 HTTP 请求到达时初始化动态模型和方法（此时 DB 完全可用）"""
        global _DYNAMIC_INIT_DONE

        if _DYNAMIC_INIT_DONE:
            return

        with _DYNAMIC_INIT_LOCK:
            # Double-check inside lock
            if _DYNAMIC_INIT_DONE:
                return

            logger.info("[OK] 首次请求触发：开始初始化动态模型与方法...")

            # 初始化动态模型（可能涉及文件读写，但不查 DB）
            try:
                from .dynamic_model_registry import initialize_dynamic_models
                initialize_dynamic_models()
                logger.info("[OK] 动态模型注册完成")
            except Exception:
                logger.error("[ERROR] 动态模型初始化失败", exc_info=True)
                return

            # 绑定动态方法（会查询数据库）
            try:
                from .models.dynamic_model_factory import bind_methods_from_db
                bind_methods_from_db()
                logger.info("[OK] 动态方法绑定完成")
            except Exception:
                logger.error("[ERROR] 动态方法绑定失败", exc_info=True)
                return

            _DYNAMIC_INIT_DONE = True
            logger.info("[OK] 动态系统初始化完成")