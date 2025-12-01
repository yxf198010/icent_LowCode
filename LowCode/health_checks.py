# lowcode/health_checks.py
from health_check.backends import BaseHealthCheckBackend
from health_check.plugins import plugin_dir


class LowCodeHealthCheck(BaseHealthCheckBackend):
    def check_status(self):
        # 示例：检查是否有至少一个动态模型注册
        try:
            from .dynamic_model_registry import DYNAMIC_MODELS
            if not DYNAMIC_MODELS:
                self.add_error(Exception("无动态模型注册"))
        except Exception as e:
            self.add_error(e, "动态模型系统异常")


plugin_dir.register(LowCodeHealthCheck)