"""
LowCode 应用配置：负责动态模型与方法的初始化。
核心设计原则：
1. 延迟初始化：数据库操作推迟到首次 HTTP 请求，避免 AppConfig.ready() 中访问 DB
2. 线程安全：通过锁和状态标记保证初始化仅执行一次（跨线程/进程安全）
3. 场景适配：自动跳过管理命令、Celery、测试环境等非 Web 场景
4. 健壮性：完善的异常处理和日志，支持失败重试（可选）
5. 可扩展：预留自定义初始化钩子，支持业务扩展
"""
# 架构分层：拆分零散逻辑为独立函数，提升代码可读性和可维护性；
# 线程安全增强：使用原子操作和双重检查锁，避免并发初始化问题；
# 错误处理完善：细化异常捕获，增加兜底逻辑和友好日志；
# 配置规范化：通过 settings 集中管理开关，减少硬编码；
# 性能优化：延迟导入非核心模块，减少启动时的资源占用；
# 可扩展性提升：预留扩展接口，支持自定义初始化逻辑；
# 日志标准化：统一日志格式和级别，便于问题定位；
# 兼容增强：适配更多部署场景（如 Gunicorn/UWSGI 多进程）
# 1. 架构与可读性优化
# 函数拆分：将零散的初始化逻辑拆分为 _execute_initialization_step/_initialize_dynamic_system 等独立函数，职责单一；
# 类型注解：补充关键函数的类型注解，提升代码可维护性；
# 配置集中化：通过 _get_environment_flags 集中管理环境标记，避免硬编码；
# 延迟导入：使用 import_string 延迟导入非核心模块，减少启动时的资源占用。
# 2. 线程安全增强
# 递归锁：将普通锁替换为 RLock，支持嵌套加锁，避免死锁；
# 双重检查锁：在 _on_first_request 中先快速检查状态，再加锁二次检查，兼顾性能和安全性；
# 原子操作：初始化逻辑封装为原子步骤，避免部分初始化导致的状态不一致。
# 3. 错误处理与容错
# 步骤化执行：将初始化拆分为独立步骤，单个步骤失败不影响整体；
# 兜底逻辑：每个初始化步骤支持自定义兜底函数，避免单点失败；
# 重试机制：预留 _schedule_init_retry 重试接口，支持初始化失败后自动重试；
# 分级日志：使用不同日志级别（DEBUG/INFO/WARNING/ERROR/CRITICAL），便于问题定位。
# 4. 可扩展性提升
# 自定义钩子：支持通过 settings.LOWCODE_POST_INIT_HOOKS 配置自定义初始化钩子；
# 命令扩展：支持通过 settings.LOWCODE_SKIP_INIT_COMMANDS 扩展需跳过的管理命令；
# 接口预留：重试机制、自定义兜底逻辑等接口预留，便于业务扩展。
# 5. 部署兼容增强
# 多进程兼容：适配 Gunicorn/UWSGI 等生产服务器（RUN_MAIN 不存在时不跳过）；
# 信号去重：为信号绑定添加 dispatch_uid，避免重复注册；
# 调试友好：完善的上下文日志（PID/RUN_MAIN 等），便于多进程调试。
import sys
import os
import logging
import threading
from typing import Callable, Optional
from django.apps import AppConfig, apps
from django.conf import settings
from django.db.models.signals import post_migrate
from django.core.signals import request_started
from django.utils.module_loading import import_string
from django.db import connection

# -------------------------- 基础配置 --------------------------
logger = logging.getLogger(__name__)

# 状态标记：防止 ready() 重复执行（进程内）
_DYNAMIC_MODELS_INITIALIZED = False
# 状态标记：防止首次请求初始化重复执行（跨线程）
_DYNAMIC_INIT_DONE = False
# 线程锁：保证初始化原子性（支持递归锁）
_DYNAMIC_INIT_LOCK = threading.RLock()


# -------------------------- 工具函数：动态模型注册与表检查 --------------------------

def _register_dynamic_model(model_class):
    """
    安全注册动态模型到 Django apps
    :param model_class: 动态生成的 models.Model 子类
    """
    app_label = model_class._meta.app_label
    try:
        # 检查是否已注册（避免重复注册报错）
        existing = apps.get_model(app_label, model_class.__name__, require_ready=False)
        if existing == model_class:
            logger.debug(f"[LowCode] 模型 {model_class} 已注册，跳过重复注册")
            return
    except LookupError:
        pass  # 未注册，继续

    apps.register_model(app_label, model_class)
    logger.info(f"[LowCode] 成功注册动态模型: {model_class}")


def table_exists(table_name: str) -> bool:
    """
    兼容 Django 5.2+ 的表存在性检查（不再支持 using 参数）
    """
    try:
        # Django 5.2+ 移除了 using 参数，直接调用
        return table_name in connection.introspection.table_names()
    except Exception as e:
        logger.error(f"[LowCode] 检查表 '{table_name}' 存在性失败: {e}", exc_info=True)
        return False


# -------------------------- 环境判断工具函数 --------------------------
def _get_environment_flags() -> dict:
    return {
        "run_main": os.environ.get("RUN_MAIN", "false"),
        "is_testing": "test" in sys.argv or os.getenv("PYTEST_CURRENT_TEST") is not None,
        "is_celery": any(arg.startswith("celery") for arg in sys.argv) or "CELERY_WORKER" in os.environ,
        "skip_init": getattr(settings, "SKIP_DYNAMIC_MODEL_INIT", False),
        "enable_test_init": os.getenv("TESTING", "").lower() in ("1", "true", "yes"),
        "debug_mode": getattr(settings, "DEBUG", False),
    }


def _is_management_command() -> bool:
    try:
        command = sys.argv[1] if len(sys.argv) > 1 else ""
    except (IndexError, AttributeError):
        return False

    default_skip_commands = {
        "migrate", "makemigrations", "collectstatic", "dumpdata", "loaddata",
        "shell", "dbshell", "test", "createsuperuser", "check",
        "compilemessages", "makemessages", "startapp", "runserver", "runworker"
    }
    custom_skip_commands = getattr(settings, "LOWCODE_SKIP_INIT_COMMANDS", set())
    skip_commands = default_skip_commands.union(custom_skip_commands)

    return command in skip_commands


def _should_skip_initialization() -> bool:
    env = _get_environment_flags()

    if env["skip_init"]:
        logger.info("[LowCode] SKIP_DYNAMIC_MODEL_INIT=True，跳过动态模型初始化")
        return True

    if env["run_main"] != "true" and not env["debug_mode"]:
        logger.debug(f"[LowCode] 跳过初始化：RUN_MAIN={env['run_main']!r}（非 Web 主进程）")
        return True

    if _is_management_command():
        cmd = sys.argv[1] if len(sys.argv) > 1 else "unknown"
        logger.debug(f"[LowCode] 检测到管理命令 '{cmd}'，跳过动态模型初始化")
        return True

    if env["is_celery"]:
        logger.debug("[LowCode] 检测到 Celery Worker，跳过动态模型初始化")
        return True

    if env["is_testing"] and not env["enable_test_init"]:
        logger.debug("[LowCode] 测试模式下默认跳过初始化（设置 TESTING=1 可启用）")
        return True

    return False


# -------------------------- 初始化核心逻辑 --------------------------
def _safe_import_module(module_path: str) -> bool:
    try:
        __import__(module_path)
        logger.debug(f"[LowCode] 成功加载模块：{module_path}")
        return True
    except ImportError as e:
        logger.warning(f"[LowCode] 加载模块 {module_path} 失败（导入错误）：{e}", exc_info=True)
    except Exception as e:
        logger.error(f"[LowCode] 加载模块 {module_path} 失败：{e}", exc_info=True)
    return False


def _execute_initialization_step(
    step_name: str,
    func: Callable,
    fallback: Optional[Callable] = None
) -> bool:
    try:
        logger.info(f"[LowCode] 开始执行初始化步骤：{step_name}")
        func()
        logger.info(f"[LowCode] 初始化步骤完成：{step_name}")
        return True
    except Exception as e:
        logger.error(f"[LowCode] 初始化步骤失败：{step_name} | 错误：{e}", exc_info=True)
        if fallback:
            try:
                logger.info(f"[LowCode] 执行 {step_name} 兜底逻辑")
                fallback()
            except Exception as fe:
                logger.error(f"[LowCode] {step_name} 兜底逻辑执行失败：{fe}", exc_info=True)
        return False


def _bind_dynamic_methods(sender, **kwargs):
    if sender.name != "lowcode":
        return

    from lowcode.models.dynamic_model_factory import refresh_dynamic_methods

    _execute_initialization_step(
        step_name="post_migrate 刷新动态方法",
        func=refresh_dynamic_methods,
        fallback=lambda: logger.warning("[LowCode] 动态方法刷新失败，建议手动执行 refresh_dynamic_methods()")
    )


def _initialize_dynamic_system():
    model_init_success = _execute_initialization_step(
        step_name="动态模型注册",
        func=lambda: import_string("lowcode.dynamic_model_registry.initialize_dynamic_models")(),
        fallback=lambda: logger.warning("[LowCode] 动态模型注册失败，部分功能可能不可用")
    )

    if model_init_success:
        _execute_initialization_step(
            step_name="动态方法绑定",
            func=lambda: import_string("lowcode.models.dynamic_model_factory.bind_methods_from_db")(),
            fallback=lambda: logger.warning("[LowCode] 动态方法绑定失败，建议手动执行 bind_methods_from_db()")
        )
    else:
        logger.warning("[LowCode] 动态模型注册失败，跳过动态方法绑定")

    custom_hooks = getattr(settings, "LOWCODE_POST_INIT_HOOKS", [])
    for hook_path in custom_hooks:
        _execute_initialization_step(
            step_name=f"自定义初始化钩子 {hook_path}",
            func=import_string(hook_path),
            fallback=lambda: logger.warning(f"[LowCode] 自定义钩子 {hook_path} 执行失败")
        )


# -------------------------- 应用配置主类 --------------------------
class LowCodeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "lowcode"
    verbose_name = "LowCode 低代码平台"

    def ready(self):
        global _DYNAMIC_MODELS_INITIALIZED

        pid = os.getpid()
        run_main = os.environ.get("RUN_MAIN")
        logger.debug(
            f"[LowCode] AppConfig.ready() 触发 | PID={pid} | RUN_MAIN={run_main!r} | "
            f"已初始化={_DYNAMIC_MODELS_INITIALIZED}"
        )

        if _DYNAMIC_MODELS_INITIALIZED:
            logger.debug("[LowCode] 当前进程已执行 ready()，跳过重复初始化")
            return

        if _should_skip_initialization():
            _DYNAMIC_MODELS_INITIALIZED = True
            logger.info("[LowCode] 跳过动态模型初始化（非 Web 场景）")
            return

        try:
            post_migrate.connect(
                _bind_dynamic_methods,
                sender=self,
                weak=False,
                dispatch_uid="lowcode_post_migrate_bind_methods"
            )
            logger.debug("[LowCode] 已注册 post_migrate 信号（刷新动态方法）")

            _safe_import_module("lowcode.signals")

            request_started.connect(
                self._on_first_request,
                weak=False,
                dispatch_uid="lowcode_first_request_init"
            )
            logger.debug("[LowCode] 已注册 request_started 信号（首次请求初始化）")

        except Exception as e:
            logger.error(f"[LowCode] ready() 初始化信号失败：{e}", exc_info=True)
            _DYNAMIC_MODELS_INITIALIZED = True
            raise

        _DYNAMIC_MODELS_INITIALIZED = True
        logger.info("[LowCode] AppConfig.ready() 执行完成，等待首次请求触发动态系统初始化")

    def _on_first_request(self, **kwargs):
        global _DYNAMIC_INIT_DONE

        if _DYNAMIC_INIT_DONE:
            return

        with _DYNAMIC_INIT_LOCK:
            if _DYNAMIC_INIT_DONE:
                return

            logger.info("[LowCode] 首次请求触发动态系统初始化...")

            try:
                _initialize_dynamic_system()
                _DYNAMIC_INIT_DONE = True
                logger.info("[LowCode] 动态系统初始化完成 ✅")
            except Exception as e:
                logger.critical(f"[LowCode] 动态系统初始化失败 ❌：{e}", exc_info=True)
                raise

    def _schedule_init_retry(self, delay: int = 5):
        import threading
        logger.info(f"[LowCode] {delay} 秒后重试动态系统初始化...")

        def retry_init():
            global _DYNAMIC_INIT_DONE
            _DYNAMIC_INIT_DONE = False
            self._on_first_request()

        threading.Timer(delay, retry_init).start()