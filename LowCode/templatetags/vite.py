# lowcode/templatetags/vite.py
import json
import os
from django import template
from django.conf import settings
from django.templatetags.static import static

register = template.Library()

def _get_manifest():
    manifest_path = os.path.join(
        settings.BASE_DIR,
        'lowcode', 'static', 'lowcode_designer', 'manifest.json'
    )
    print("🔍 Manifest path:", manifest_path)  # 👈 临时加这行
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(f"Manifest not found at {manifest_path}")
    with open(manifest_path, 'r', encoding='utf-8') as f:
        return json.load(f)

@register.simple_tag
def vite_entry(entry_name='src/main.js'):
    print("🔥 vite_entry called!")
    try:
        manifest = _get_manifest()
        entry = manifest[entry_name]
        tags = []
        for css in entry.get('css', []):
            url = static(f"lowcode_designer/{css}")
            tags.append(f'<link rel="stylesheet" href="{url}">')
        js_url = static(f"lowcode_designer/{entry['file']}")
        tags.append(f'<script type="module" src="{js_url}"></script>')
        result = '\n'.join(tags)
        print("✅ vite_entry output:", result)  # 👈 临时加这行
        return result
    except Exception as e:
        print("❌ vite_entry error:", str(e))
        raise  # 👈 开发阶段不要静默，要报错！