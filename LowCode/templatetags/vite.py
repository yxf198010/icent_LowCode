# lowcode/templatetags/vite.py
import os
from django import template
from django.conf import settings
from django.templatetags.static import static
from django.utils.safestring import mark_safe
from lowcode.utils.vite import get_vite_asset

register = template.Library()


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
        assets = get_vite_asset(
            app_name="lowcode_designer",
            entry_key=entry_name,
            dev_fallback=None  # 生产环境不用 fallback
        )
        tags = []
        for css in assets.get('css', []):
            tags.append(f'<link rel="stylesheet" href="{static(css)}">')
        tags.append(f'<script type="module" src="{static(assets["js"])}"></script>')
        return mark_safe('\n'.join(tags))

    except Exception as e:
        # 开发阶段抛出错误便于调试，生产环境可根据需要改为静默或记录日志
        error_msg = f"[vite_entry] Failed to load asset for '{entry_name}': {str(e)}"
        if settings.DEBUG:
            raise RuntimeError(error_msg) from e
        else:
            # 生产环境可选：返回空或占位符（避免页面崩溃）
            return mark_safe(f"<!-- {error_msg} -->")