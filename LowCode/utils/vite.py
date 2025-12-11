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

    - 开发环境（settings.DEBUG=True）：直接返回 dev_fallback（不读磁盘）
    - 生产环境：通过 staticfiles 查找 manifest.json

    Args:
        app_name: 应用名称，用于构造默认 manifest 路径
        manifest_path: 自定义 manifest 相对于 STATIC_ROOT/STATICFILES_DIRS 的路径
        dev_fallback: 开发模式下返回的模拟 manifest 数据

    Returns:
        dict: manifest.json 的解析内容
    """
    if settings.DEBUG:
        if dev_fallback is None:
            # 默认开发 fallback（仅用于类型兼容，实际开发时由 vite dev server 提供资源）
            dev_fallback = {
                "src/main.js": {
                    "file": "assets/main.js",
                    "css": ["assets/main.css"],
                    "imports": []
                }
            }
        return dev_fallback

    # 生产环境必须读取真实 manifest
    if manifest_path is None:
        # 默认路径：app_name/.vite/manifest.json
        # 注意：Vite 默认输出到 .vite/manifest.json（相对于 outDir）
        manifest_path = os.path.join(app_name, ".vite", "manifest.json")

    full_path = finders.find(manifest_path)
    if not full_path or not os.path.isfile(full_path):
        raise ImproperlyConfigured(
            f"Vite manifest 文件未找到: '{manifest_path}'。\n"
            "请确认已执行 `npm run build` 并运行 `python manage.py collectstatic`。"
        )

    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError, json.JSONDecodeError) as e:
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

    注意：此函数在开发模式下也会返回 fallback 路径（但模板标签应优先使用 Vite Dev Server），
    因此调用方需根据环境决定是否使用本函数。
    """
    manifest = get_vite_manifest(
        app_name=app_name,
        manifest_path=manifest_path,
        dev_fallback=dev_fallback
    )

    if entry_key not in manifest:
        available = ', '.join(manifest.keys())
        raise ImproperlyConfigured(
            f"Vite manifest 中未找到入口 '{entry_key}'。可用入口: [{available}]"
        )

    entry = manifest[entry_key]
    js_file = entry.get("file")
    if not js_file:
        raise ImproperlyConfigured(
            f"入口 '{entry_key}' 在 manifest 中缺少 'file' 字段。"
        )

    # 构造 static 可识别的路径：app_name + asset 路径
    # 使用正斜杠确保跨平台兼容
    js_url = os.path.join(app_name, js_file).replace("\\", "/")
    css_urls = [
        os.path.join(app_name, css).replace("\\", "/")
        for css in entry.get("css", [])
    ]

    return {
        "js": js_url,
        "css": css_urls
    }