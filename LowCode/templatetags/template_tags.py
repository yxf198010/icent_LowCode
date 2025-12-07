# lowcode/templatetags/template_tags.py
from django import template
from django.template.loader import get_template, TemplateDoesNotExist

register = template.Library()


@register.simple_tag
def get_template_exists(template_name, as_var=None):
    """
    自定义标签：判断模板是否存在（支持赋值给变量）
    使用方式：{% get_template_exists "xxx.html" as var_name %}
    """
    try:
        get_template(template_name)
        result = True
    except TemplateDoesNotExist:
        result = False

    # 赋值给模板变量
    if as_var:
        context = template.RequestContext(template.Variable('request').resolve(template.Context()))
        context[as_var] = result
        return ''
    return result