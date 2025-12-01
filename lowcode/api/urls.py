# lowcode/api/urls.py
"""
低代码平台 API 路由模块。

提供两大核心功能接口：
1. 动态数据库表创建（同步 DDL）
2. 动态模型字段升级（异步任务）

所有接口均位于 `/api/lowcode/` 前缀下（由主 urls.py 挂载）。
"""

from django.urls import path
from lowcode.api.views import (  # 绝对导入，推荐
    api_root,
    DynamicTableCreateView,
    UpgradeModelAPIView,
    UpgradeStatusAPIView,
    UpgradeHistoryAPIView,
)

app_name = 'lowcode_api'

urlpatterns = [
    path('', api_root, name='api-root'),
    path('create-table/', DynamicTableCreateView.as_view(), name='dynamic-create-table'),
    path('upgrade-model/', UpgradeModelAPIView.as_view(), name='upgrade-model'),
    path('upgrade-status/<str:task_id>/', UpgradeStatusAPIView.as_view(), name='upgrade-status'),
    path('upgrade-history/', UpgradeHistoryAPIView.as_view(), name='upgrade-history'),
]