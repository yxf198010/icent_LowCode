"""
动态方法卸载器：安全卸载通过配置绑定到动态模型的自定义方法。
仅卸载标记为动态注入的方法，避免误删原生或业务方法。
"""
import logging
import threading
import time
from typing import Set, Tuple, Dict, Optional, Type, Any
from collections import defaultdict
from django.apps import apps
from django.db import models, transaction
from django.db.models import QuerySet
from django.core.exceptions import ValidationError

# ✅ 修复：LookupError 是Python内置异常，无需导入
# 修正导入路径 & 兼容导入
try:
    from lowcode.models.models import MethodLowCode
    from lowcode.models.dynamic_model_factory import DYNAMIC_METHOD_PREFIX
except ImportError:
    # 兼容旧路径
    from .models import MethodLowCode
    from .dynamic_model_factory import DYNAMIC_METHOD_PREFIX

logger = logging.getLogger(__name__)

# ==================== 核心配置（可通过Django Settings覆盖） ====================
# 锁超时时间（秒）：防止死锁
UNBIND_LOCK_TIMEOUT = 10
# 是否在卸载后清理ContentType缓存
CLEAR_CONTENT_TYPE_CACHE = True
# 批量卸载时的批次大小（避免一次性处理过多数据）
BATCH_SIZE = 100

# ==================== 类型兼容处理（解决.pyi文件引用问题） ====================
# 显式声明内置异常类型，供类型检查工具识别
BuiltinLookupError = LookupError  # 别名，解决.pyi文件找不到的问题


# ==================== 线程锁：支持超时控制的递归锁 ====================
class TimeoutRLock:
    """带超时控制的递归锁，防止死锁"""

    def __init__(self, timeout: float = UNBIND_LOCK_TIMEOUT):
        self._lock = threading.RLock()
        self.timeout = timeout

    def acquire(self) -> bool:
        """获取锁，超时返回False"""
        try:
            return self._lock.acquire(timeout=self.timeout)
        except threading.TimeoutError:
            return False

    def release(self):
        """释放锁（兼容未获取到锁的情况）"""
        try:
            self._lock.release()
        except RuntimeError:
            pass  # 未获取到锁，忽略

    def __enter__(self):
        if not self.acquire():
            raise TimeoutError(f"获取锁超时（{self.timeout}秒）")
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.release()


# 全局卸载锁
_UNBIND_LOCK = TimeoutRLock()


# ==================== 工具函数 ====================
def get_dynamic_model(model_name: str) -> Optional[Type[models.Model]]:
    """安全获取动态模型类（带缓存检查）"""
    try:
        # ✅ 修复：直接捕获Python内置的LookupError
        model = apps.get_model("lowcode", model_name)
        # 校验是否为动态模型（通过表名前缀/元信息）
        if hasattr(model._meta, 'db_table') and model._meta.db_table.startswith('lowcode_'):
            return model
        logger.warning(f"模型 '{model_name}' 不是低代码动态模型，跳过")
        return None
    except BuiltinLookupError:  # 使用别名，解决类型注解问题
        logger.debug(f"模型 '{model_name}' 未注册或不存在")
        return None
    except Exception as e:
        logger.error(f"获取模型 '{model_name}' 失败: {e}", exc_info=True)
        return None


def is_dynamic_method(model: Type[models.Model], method_name: str) -> bool:
    """判断方法是否为动态绑定的方法"""
    internal_attr = f"{DYNAMIC_METHOD_PREFIX}{method_name}"
    return hasattr(model, internal_attr)


# ==================== 核心卸载逻辑 ====================
def _safe_delete_method(dynamic_model: Type[models.Model], method_name: str) -> bool:
    """
    安全删除一个动态方法及其内部实现。
    仅当存在内部标记属性（_dyn_method_xxx）时才视为本系统绑定的方法。

    :param dynamic_model: 目标模型类
    :param method_name: 公开方法名（如 'calculate_total'）
    :return: 是否成功删除
    """
    if not isinstance(dynamic_model, type) or not issubclass(dynamic_model, models.Model):
        logger.error(f"无效的模型类: {dynamic_model}")
        return False

    internal_attr = f"{DYNAMIC_METHOD_PREFIX}{method_name}"
    model_name = dynamic_model.__name__

    # 严格判断：必须存在内部实现才允许卸载
    if not is_dynamic_method(dynamic_model, method_name):
        logger.debug(f"跳过卸载：{model_name}.{method_name} 不是动态绑定方法（无内部标记）")
        return False

    try:
        # 1. 删除内部实现
        if hasattr(dynamic_model, internal_attr):
            delattr(dynamic_model, internal_attr)
            logger.debug(f"删除内部实现属性: {model_name}.{internal_attr}")

        # 2. 删除公开代理方法（如果还存在）
        if hasattr(dynamic_model, method_name):
            delattr(dynamic_model, method_name)
            logger.debug(f"删除公开代理方法: {model_name}.{method_name}")

        # 3. 清理模型类的__dict__缓存（Django内部缓存）
        if method_name in dynamic_model.__dict__:
            del dynamic_model.__dict__[method_name]
        if internal_attr in dynamic_model.__dict__:
            del dynamic_model.__dict__[internal_attr]

        logger.info(f"✅ 成功卸载动态方法: {model_name}.{method_name}")
        return True

    except AttributeError as e:
        # 可能已被其他线程删除，视为成功
        logger.debug(
            f"卸载方法时检测到属性已不存在（可能并发操作）: "
            f"{model_name}.{method_name} - {e}"
        )
        return True
    except Exception as e:
        logger.error(
            f"❌ 卸载动态方法时发生未预期错误: "
            f"{model_name}.{method_name} - {e}",
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
    # 前置校验
    if not model_name or not method_name:
        logger.error("模型名和方法名不能为空")
        return False

    # 获取动态模型
    dynamic_model = get_dynamic_model(model_name)
    if not dynamic_model:
        logger.warning(f"⚠️ 卸载失败：模型 '{model_name}' 不存在/未注册/非动态模型")
        return False

    # 加锁执行卸载
    try:
        with _UNBIND_LOCK:
            return _safe_delete_method(dynamic_model, method_name)
    except TimeoutError:
        logger.error(f"获取卸载锁超时，无法卸载 {model_name}.{method_name}")
        return False


def unbind_methods_by_model(model_name: str, batch_size: int = BATCH_SIZE) -> int:
    """
    卸载某个动态模型的所有配置化动态方法。

    :param model_name: 模型类名
    :param batch_size: 批次大小
    :return: 成功卸载的方法数量
    """
    # 前置校验
    if not model_name:
        logger.error("模型名不能为空")
        return 0

    # 获取动态模型
    dynamic_model = get_dynamic_model(model_name)
    if not dynamic_model:
        logger.warning(f"⚠️ 模型 '{model_name}' 不存在/未注册/非动态模型，跳过卸载")
        return 0

    # 获取该模型所有曾配置的方法名（去重）
    try:
        method_qs: QuerySet = MethodLowCode.objects.filter(model_name=model_name)
        method_names: Set[str] = set()

        # 分批获取，避免大数据量内存溢出
        for offset in range(0, method_qs.count(), batch_size):
            batch_names = method_qs.values_list("method_name", flat=True).distinct()[offset:offset + batch_size]
            method_names.update(batch_names)

    except Exception as e:
        logger.error(f"获取模型 '{model_name}' 的方法配置失败: {e}", exc_info=True)
        return 0

    if not method_names:
        logger.debug(f"📦 模型 '{model_name}' 无配置方法，无需卸载")
        return 0

    # 加锁批量卸载
    unloaded_count = 0
    try:
        with _UNBIND_LOCK:
            for name in method_names:
                if _safe_delete_method(dynamic_model, name):
                    unloaded_count += 1

            # 清理ContentType缓存
            if CLEAR_CONTENT_TYPE_CACHE:
                try:
                    from django.contrib.contenttypes.models import ContentType
                    ContentType.objects.clear_cache()
                    logger.debug(f"清理 {model_name} 的ContentType缓存")
                except Exception:
                    pass

    except TimeoutError:
        logger.error(f"获取卸载锁超时，部分方法可能未卸载")
    except Exception as e:
        logger.error(f"批量卸载 {model_name} 方法失败: {e}", exc_info=True)

    logger.info(f"📦 模型 '{model_name}' 的动态方法卸载完成，共移除 {unloaded_count}/{len(method_names)} 个方法")
    return unloaded_count


def unbind_methods_from_db(
        batch_size: int = BATCH_SIZE,
        clear_content_type: bool = CLEAR_CONTENT_TYPE_CACHE
) -> int:
    """
    全局卸载：卸载所有通过 MethodLowCode 配置的动态方法。
    安全遍历所有涉及的模型，仅删除带 _dyn_method_ 前缀的内部方法及对应代理。

    :param batch_size: 批次大小
    :param clear_content_type: 是否清理ContentType缓存
    :return: 成功卸载的方法数量
    """
    # 快速检查是否存在任何配置
    try:
        if not MethodLowCode.objects.filter(is_active=True).exists():
            logger.info("📭 无启用的动态方法配置，无需卸载")
            return 0
    except Exception as e:
        logger.error(f"检查动态方法配置失败: {e}", exc_info=True)
        return 0

    # 获取所有唯一 (model_name, method_name) 对（仅启用的）
    try:
        config_qs: QuerySet = MethodLowCode.objects.filter(is_active=True).only("model_name", "method_name")
        config_pairs: Tuple[Tuple[str, str]] = tuple(config_qs.values_list("model_name", "method_name").distinct())
    except Exception as e:
        logger.error(f"获取动态方法配置列表失败: {e}", exc_info=True)
        return 0

    if not config_pairs:
        logger.info("📭 无动态方法配置记录，无需卸载")
        return 0

    # 按模型分组
    model_to_methods: Dict[str, Set[str]] = defaultdict(set)
    for model_name, method_name in config_pairs:
        model_to_methods[model_name].add(method_name)

    # 加锁执行全局卸载
    total_unloaded = 0
    failed_models = []

    try:
        with _UNBIND_LOCK:
            for model_name, method_names in model_to_methods.items():
                # 获取动态模型
                dynamic_model = get_dynamic_model(model_name)
                if not dynamic_model:
                    failed_models.append(model_name)
                    continue

                # 分批卸载方法
                for idx in range(0, len(method_names), batch_size):
                    batch_methods = list(method_names)[idx:idx + batch_size]
                    for method_name in batch_methods:
                        if _safe_delete_method(dynamic_model, method_name):
                            total_unloaded += 1

            # 全局清理ContentType缓存
            if clear_content_type:
                try:
                    from django.contrib.contenttypes.models import ContentType
                    ContentType.objects.clear_cache()
                    logger.debug("清理全局ContentType缓存")
                except Exception as e:
                    logger.warning(f"清理ContentType缓存失败: {e}")

    except TimeoutError:
        logger.error(f"获取全局卸载锁超时，部分方法可能未卸载")
    except Exception as e:
        logger.error(f"全局卸载动态方法失败: {e}", exc_info=True)

    # 日志汇总
    if failed_models:
        logger.warning(f"❌ 以下模型卸载失败: {', '.join(failed_models)}")
    logger.info(f"🧹 全局动态方法卸载完成，共移除 {total_unloaded}/{len(config_pairs)} 个方法")

    return total_unloaded


def unbind_methods_by_ids(method_ids: list[int]) -> Tuple[int, int]:
    """
    根据MethodLowCode的ID卸载指定的动态方法（扩展功能）。

    :param method_ids: MethodLowCode的ID列表
    :return: (成功数量, 失败数量)
    """
    if not method_ids:
        logger.warning("方法ID列表为空")
        return 0, 0

    success_count = 0
    fail_count = 0

    try:
        with _UNBIND_LOCK, transaction.atomic():
            # 获取方法配置
            methods = MethodLowCode.objects.filter(id__in=method_ids).select_related()
            for method in methods:
                if unbind_single_method(method.model_name, method.method_name):
                    success_count += 1
                    # 标记为禁用（可选）
                    method.is_active = False
                    method.save(update_fields=["is_active"])
                else:
                    fail_count += 1

    except Exception as e:
        logger.error(f"按ID卸载方法失败: {e}", exc_info=True)
        fail_count = len(method_ids) - success_count

    logger.info(f"🎯 按ID卸载完成：成功{success_count}个，失败{fail_count}个")
    return success_count, fail_count


# ==================== 缓存清理工具（扩展功能） ====================
def clear_dynamic_model_cache(model_name: Optional[str] = None):
    """
    清理动态模型相关缓存。

    :param model_name: 模型名（None表示清理所有）
    """
    try:
        with _UNBIND_LOCK:
            # 清理AppRegistry缓存
            apps.clear_cache()
            logger.debug("清理Django AppRegistry缓存")

            # 清理ContentType缓存
            if CLEAR_CONTENT_TYPE_CACHE:
                from django.contrib.contenttypes.models import ContentType
                if model_name:
                    # ✅ 再次确认：捕获内置LookupError
                    try:
                        ContentType.objects.clear_cache(model=model_name)
                        logger.debug(f"清理 {model_name} 的ContentType缓存")
                    except BuiltinLookupError:
                        logger.debug(f"模型 {model_name} 无ContentType缓存，跳过")
                    except Exception as e:
                        logger.warning(f"清理 {model_name} ContentType缓存失败: {e}")
                else:
                    ContentType.objects.clear_cache()
                    logger.debug("清理全局ContentType缓存")

            # 清理模型类的__dict__缓存（如果指定模型）
            if model_name:
                dynamic_model = get_dynamic_model(model_name)
                if dynamic_model:
                    dynamic_model.__dict__.clear()
                    logger.debug(f"清理 {model_name} 的类缓存")

    except Exception as e:
        logger.error(f"清理动态模型缓存失败: {e}", exc_info=True)