# lowcode/templatetags/vite.py
import json
import os
from django import template
from django.conf import settings
from django.templatetags.static import static
from django.utils.safestring import mark_safe

register = template.Library()


def _get_manifest():
    """读取 Vite 构建生成的 manifest.json 文件"""
    manifest_path = os.path.join(
        settings.BASE_DIR,
        'lowcode', 'static', 'lowcode_designer', 'manifest.json'
    )
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(f"Vite manifest not found at {manifest_path}. "
                                f"Did you run 'npm run build' in the frontend directory?")
    with open(manifest_path, 'r', encoding='utf-8') as f:
        return json.load(f)


@register.simple_tag
def vite_entry(entry_name='src/main.js'):
    """
    根据当前环境自动注入 Vite 资源：
    - 开发模式（DEBUG=True 且 VITE_DEV_MODE=True）：连接 Vite Dev Server
    - 生产模式：使用构建后的静态文件（通过 manifest.json）

    用法：
      {% load vite %}
      {% vite_entry "src/main.js" %}
    """
    # === 开发模式：使用 Vite Dev Server（支持热更新）===
    if settings.DEBUG and getattr(settings, 'VITE_DEV_MODE', True):
        dev_server_url = getattr(settings, 'VITE_DEV_SERVER_URL', 'http://localhost:5173')
        script_tag = f'<script type="module" src="{dev_server_url}/{entry_name}"></script>'
        return mark_safe(script_tag)

    # === 生产模式：使用构建产物 ===
    try:
        manifest = _get_manifest()
        if entry_name not in manifest:
            available = ', '.join(manifest.keys())
            raise KeyError(
                f"Entry '{entry_name}' not found in manifest. Available entries: {available}"
            )

        entry = manifest[entry_name]
        tags = []

        # 注入 CSS（如果有）
        for css in entry.get('css', []):
            url = static(f"lowcode_designer/{css}")
            tags.append(f'<link rel="stylesheet" href="{url}">')

        # 注入 JS
        js_url = static(f"lowcode_designer/{entry['file']}")
        tags.append(f'<script type="module" src="{js_url}"></script>')

        return mark_safe('\n'.join(tags))

    except Exception as e:
        # 开发阶段抛出错误便于调试，生产环境可根据需要改为静默或记录日志
        error_msg = f"[vite_entry] Failed to load asset for '{entry_name}': {str(e)}"
        if settings.DEBUG:
            raise RuntimeError(error_msg) from e
        else:
            # 生产环境可选：返回空或占位符（避免页面崩溃）
            return mark_safe(f"<!-- {error_msg} -->")