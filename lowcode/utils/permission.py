# utils/permission.py
# 数据权限精细化：基于“用户-模型-数据ID”三级授权，确保用户仅能操作自己有权限的数据；
# 权限校验分层：先校验“是否能调用方法”（角色权限），再校验“是否能访问该数据”（数据权限），逻辑清晰。
from django.core.exceptions import PermissionDenied

def check_method_permission(user, model_name, method_name):
    """
    校验用户是否有权调用动态方法
    :param user: 当前登录用户（需关联角色，如 user.roles.all()）
    :param model_name: 动态模型类名
    :param method_name: 动态方法名
    :raises PermissionDenied: 无权限时抛出异常
    """
    from ..models import MethodLowCode

    # 获取方法配置（含关联角色）
    try:
        method_config = MethodLowCode.objects.get(
            method_name=method_name,
            model_name=model_name,
            is_active=True
        )
    except MethodLowCode.DoesNotExist:
        raise PermissionDenied(f"方法 {method_name} 不存在或已禁用")

    # 校验用户角色是否在允许列表中（用户需有 roles 关联字段）
    user_roles = user.roles.all()
    if not method_config.roles.filter(id__in=user_roles.values_list("id", flat=True)).exists():
        raise PermissionDenied(f"用户无权限调用方法 {method_name}")



def check_data_permission(user, model_instance):
    """
    校验用户是否有权访问该数据实例
    :param user: 当前用户
    :param model_instance: 动态模型实例（如DynamicOrder对象）
    :raises PermissionDenied: 无数据权限时抛出异常
    """
    from ..models.models import DataPermission

    # 超级管理员跳过数据权限校验（拥有所有数据权限）
    if user.is_superuser:
        return

    model_name = model_instance.__class__.__name__
    data_id = str(model_instance.id)  # 数据ID（转为字符串存储）

    # 校验用户是否有该数据的访问权限
    has_perm = DataPermission.objects.filter(
        user=user,
        model_name=model_name,
        data_id=data_id
    ).exists()

    if not has_perm:
        raise PermissionDenied(f"用户无权限访问 {model_name} 数据（ID：{data_id}）")