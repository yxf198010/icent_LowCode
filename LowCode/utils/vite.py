# lowcode/utils/vite.py
import json
import os
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.contrib.staticfiles import finders


def get_vite_manifest(
        app_name: str = "lowcode_designer",
        manifest_path: str = None,
        dev_fallback: dict = None
) -> dict:
    """
    获取 Vite 构建生成的 manifest.json 内容。

    如果 settings.DEBUG 为 True，直接返回 dev_fallback（不读磁盘）。
    否则尝试从 staticfiles 中查找 manifest.json。
    """
    if settings.DEBUG:
        # 开发环境：不读取 manifest，直接返回 fallback
        if dev_fallback is None:
            dev_fallback = {
                "src/main.js": {
                    "file": "assets/main.js",
                    "css": ["assets/main.css"],
                    "imports": []
                }
            }
        return dev_fallback

    # 生产环境：必须读取 manifest
    if manifest_path is None:
        manifest_path = os.path.join(app_name, ".vite", "manifest.json")

    full_path = finders.find(manifest_path)
    if not full_path or not os.path.exists(full_path):
        raise ImproperlyConfigured(
            f"Vite manifest 文件未找到: '{manifest_path}'。"
            "请确认已执行 `npm run build` 并运行 `python manage.py collectstatic`。"
        )

    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise ImproperlyConfigured(
            f"无法读取或解析 Vite manifest 文件 '{full_path}': {e}"
        )


def get_vite_asset(
        app_name: str = "lowcode_designer",
        entry_key: str = "src/main.js",
        manifest_path: str = None,
        dev_fallback: dict = None
) -> dict:
    """
    返回可用于 {% static %} 的资源路径（已包含 app_name 前缀）。

    示例返回：
    {
        "js": "lowcode_designer/assets/main.xxx.js",
        "css": ["lowcode_designer/assets/main.xxx.css"]
    }
    """
    manifest = get_vite_manifest(
        app_name=app_name,
        manifest_path=manifest_path,
        dev_fallback=dev_fallback
    )

    entry_data = manifest.get(entry_key)
    if not entry_data:
        raise ImproperlyConfigured(
            f"Vite manifest 中未找到入口 '{entry_key}'，可用入口: {list(manifest.keys())}"
        )

    # ✅ 关键：拼接 app_name + file/css，生成 Django static 可识别的路径
    js_url = os.path.join(app_name, entry_data["file"]).replace("\\", "/")
    css_urls = [
        os.path.join(app_name, css).replace("\\", "/")
        for css in entry_data.get("css", [])
    ]

    return {
        "js": js_url,
        "css": css_urls
    }