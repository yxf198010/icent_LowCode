# celery.py
import os
from celery import Celery

# 初始化Django环境
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'Icent_LowCode.settings')

app = Celery('Icent_LowCode')
# 从Django配置中读取Celery配置
app.config_from_object('django.conf:settings', namespace='CELERY')
# 自动发现任务
app.autodiscover_tasks()