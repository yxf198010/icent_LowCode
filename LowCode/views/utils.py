# views/utils.py
from django.http import JsonResponse
from django.middleware.csrf import get_token

def get_csrf_token(request):
    """
    用于前后端分离架构中，前端主动获取 CSRF Cookie。
    调用此接口会触发 Set-Cookie: csrftoken=...
    """
    get_token(request)  # 关键：触发设置 Cookie
    return JsonResponse({'detail': 'CSRF cookie set'})