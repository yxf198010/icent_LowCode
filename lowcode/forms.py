# lowcode/forms.py
from django import forms
from django.core.exceptions import ValidationError
from lowcode.models.models import ModelLowCode
from .utils.validators import (
    validate_model_name,
    validate_table_name_format,
)
from .utils.naming import generate_table_name_from_model
from .utils.model_naming import (
    is_model_name_unique,
    is_table_name_unique,
    ensure_unique_table_name,
)


class ModelLowCodeForm(forms.ModelForm):
    class Meta:
        model = ModelLowCode
        fields = ['name', 'table_name']
        widgets = {
            'name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': '例如：用户、订单、产品'
            }),
            'table_name': forms.TextInput(attrs={
                'class': 'form-control',
                'placeholder': '留空将自动生成（如 lowcode_user）'
            }),
        }
        help_texts = {
            'name': '模型名称，用于代码引用和显示（如“用户”）',
            'table_name': '数据库表名，必须符合标识符规范（字母、数字、下划线），建议小写',
        }

    def clean_name(self):
        name = self.cleaned_data.get('name', '').strip()
        if not name:
            raise ValidationError("模型名称不能为空")
        validate_model_name(name)
        exclude_id = self.instance.pk
        if not is_model_name_unique(name, exclude_id):
            raise ValidationError(f'模型名称 "{name}" 已存在，请使用其他名称')
        return name

    def clean_table_name(self):
        table_name = self.cleaned_data.get('table_name', '').strip()
        name = self.cleaned_data.get('name')

        # 若未提供表名但有模型名，则自动生成
        if not table_name and name:
            table_name = generate_table_name_from_model(name)

        # 若仍为空（如 name 也为空），则设为空字符串（后续由 clean 处理）
        if not table_name:
            return ''

        validate_table_name_format(table_name)

        exclude_id = self.instance.pk
        if not is_table_name_unique(table_name, exclude_id):
            raise ValidationError(f'数据表名 "{table_name}" 已被其他模型使用')

        return table_name

    def clean(self):
        cleaned_data = super().clean()
        name = cleaned_data.get('name')
        table_name = cleaned_data.get('table_name')

        # 最终确保：只要有 name，就必须有合法 table_name
        if name and not table_name:
            base = generate_table_name_from_model(name)
            exclude_id = self.instance.pk
            unique_table_name = ensure_unique_table_name(base, exclude_id)
            cleaned_data['table_name'] = unique_table_name

        # 额外校验：若无 name 但有 table_name，视为异常
        if not name and table_name:
            raise ValidationError("必须先设置模型名称才能指定表名")

        return cleaned_data