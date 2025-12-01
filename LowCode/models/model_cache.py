# lowcode/models/model_cache.py
"""
动态方法卸载器：安全卸载通过配置绑定到动态模型的自定义方法。
仅卸载标记为动态注入的方法，避免误删原生或业务方法。
"""
# 管理动态模型的 注册、缓存、卸载（如清理 apps.registry）
import logging
import threading
from typing import Set, Tuple
from collections import defaultdict
from django.apps import apps
from django.db import models

# ✅ 修正导入路径：MethodLowCode 应位于 models.models
from lowcode.models.models import MethodLowCode
from .dynamic_model_factory import DYNAMIC_METHOD_PREFIX

logger = logging.getLogger(__name__)

# 共享锁：与绑定模块协同，防止并发竞争（建议与 bind 模块共用同一锁）
_UNBIND_LOCK = threading.RLock()


def _safe_delete_method(dynamic_model: type[models.Model], method_name: str) -> bool:
    """
    安全删除一个动态方法及其内部实现。
    仅当存在内部标记属性（_dyn_method_xxx）时才视为本系统绑定的方法。

    :param dynamic_model: 目标模型类
    :param method_name: 公开方法名（如 'calculate_total'）
    :return: 是否成功删除
    """
    internal_attr = f"{DYNAMIC_METHOD_PREFIX}{method_name}"

    # 严格判断：必须存在内部实现才允许卸载
    if not hasattr(dynamic_model, internal_attr):
        return False

    try:
        # 删除内部实现
        delattr(dynamic_model, internal_attr)
        # 删除公开代理方法（如果还存在）
        if hasattr(dynamic_model, method_name):
            delattr(dynamic_model, method_name)
        return True
    except AttributeError as e:
        # 可能已被其他线程或代码删除，视为成功
        logger.debug(
            f"卸载方法时检测到属性已不存在（可能并发操作）: "
            f"{dynamic_model.__name__}.{method_name} - {e}"
        )
        return True
    except Exception as e:
        logger.error(
            f"❌ 卸载动态方法时发生未预期错误: "
            f"{dynamic_model.__name__}.{method_name} - {e}",
            exc_info=True
        )
        return False


def unbind_single_method(model_name: str, method_name: str) -> bool:
    """
    卸载单个动态方法（仅当它是通过配置动态绑定的）。

    :param model_name: 动态模型类名（如 "SalesOrder"）
    :param method_name: 要卸载的方法名（如 "calculate_total"）
    :return: 是否成功卸载
    """
    try:
        dynamic_model: type[models.Model] = apps.get_model("lowcode", model_name)
    except LookupError:
        logger.warning(f"⚠️ 卸载失败：模型 '{model_name}' 不存在或未注册")
        return False

    if _safe_delete_method(dynamic_model, method_name):
        logger.info(f"✅ 成功卸载动态方法: {model_name}.{method_name}")
        return True
    else:
        logger.debug(f"⏭️ 跳过卸载：{model_name}.{method_name} 不是动态绑定方法（无内部标记）")
        return False


def unbind_methods_by_model(model_name: str) -> int:
    """
    卸载某个动态模型的所有配置化动态方法。

    :param model_name: 模型类名
    :return: 成功卸载的方法数量
    """
    try:
        dynamic_model: type[models.Model] = apps.get_model("lowcode", model_name)
    except LookupError:
        logger.warning(f"⚠️ 模型 '{model_name}' 不存在或未注册，跳过卸载")
        return 0

    # 获取该模型所有曾配置的方法名（去重）
    method_names: Set[str] = set(
        MethodLowCode.objects
        .filter(model_name=model_name)
        .values_list("method_name", flat=True)
        .distinct()
    )

    if not method_names:
        logger.debug(f"📦 模型 '{model_name}' 无配置方法，无需卸载")
        return 0

    unloaded_count = 0
    for name in method_names:
        if _safe_delete_method(dynamic_model, name):
            unloaded_count += 1

    logger.info(f"📦 模型 '{model_name}' 的动态方法卸载完成，共移除 {unloaded_count} 个方法")
    return unloaded_count


def unbind_methods_from_db() -> int:
    """
    全局卸载：卸载所有通过 MethodLowCode 配置的动态方法。
    安全遍历所有涉及的模型，仅删除带 _dyn_method_ 前缀的内部方法及对应代理。
    """
    with _UNBIND_LOCK:
        # 快速检查是否存在任何配置
        if not MethodLowCode.objects.filter(is_active=True).exists():
            logger.info("📭 无启用的动态方法配置，无需卸载")
            return 0

        # 获取所有唯一 (model_name, method_name) 对（仅启用的）
        config_pairs = (
            MethodLowCode.objects
            .filter(is_active=True)
            .only("model_name", "method_name")  # 性能优化
            .values_list("model_name", "method_name")
            .distinct()
        )

        if not config_pairs:
            logger.info("📭 无动态方法配置记录，无需卸载")
            return 0

        # 按模型分组
        model_to_methods: dict[str, Set[str]] = defaultdict(set)
        for model_name, method_name in config_pairs:
            model_to_methods[model_name].add(method_name)

        total_unloaded = 0
        for model_name, method_names in model_to_methods.items():
            try:
                dynamic_model: type[models.Model] = apps.get_model("lowcode", model_name)
            except LookupError:
                logger.debug(f"🔍 模型 '{model_name}' 未注册或已删除，跳过卸载其方法")
                continue

            for method_name in method_names:
                if _safe_delete_method(dynamic_model, method_name):
                    total_unloaded += 1

        logger.info(f"🧹 全局动态方法卸载完成，共移除 {total_unloaded} 个方法")
        return total_unloaded