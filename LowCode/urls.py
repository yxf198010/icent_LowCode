from django.urls import path
from LowCode.views import GenericListView, GenericCreateView

urlpatterns = [
    # 列表页：/config/list/1/（1为模型配置ID）
    path('list/<int:model_id>/', GenericListView.as_view(), name='generic_list'),
    # 创建接口：/config/create/1/
    path('create/<int:model_id>/', GenericCreateView.as_view(), name='generic_create'),
]