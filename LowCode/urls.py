from django.urls import path
from LowCode.views import GenericListView, GenericCreateView, LowCodeHomeView  # 导入首页视图

urlpatterns = [
    # 新增：应用首页（匹配 /LowCode/）
    path('', LowCodeHomeView.as_view(), name='lowcode_home'),  # 空路径 = 应用根路径
    # 列表页：/lowcode/list/1/（1为模型配置ID）
    path('list/<int:model_id>/', GenericListView.as_view(), name='generic_list'),
    # 创建接口：/lowcode/create/1/
    path('create/<int:model_id>/', GenericCreateView.as_view(), name='generic_create'),
]