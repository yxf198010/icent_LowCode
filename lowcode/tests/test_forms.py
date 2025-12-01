# tests/test_forms.py
from django.test import TestCase
from your_app.models import ModelLowCode
from your_app.forms import ModelLowCodeForm


class ModelLowCodeFormTest(TestCase):
    def setUp(self):
        # 创建一个已有模型用于编辑测试
        self.existing = ModelLowCode.objects.create(
            name='UserGroup',
            table_name='lowcode_usergroup',
            fields='[{"name":"title","type":"CharField","kwargs":{"max_length":100}}]'
        )

    def test_valid_form(self):
        data = {
            'name': 'Product',
            'table_name': '',
            'fields': '''
                [
                    {"name": "title", "type": "CharField", "kwargs": {"max_length": 200}},
                    {"name": "is_active", "type": "BooleanField", "kwargs": {"default": true}}
                ]
            '''
        }
        form = ModelLowCodeForm(data=data)
        self.assertTrue(form.is_valid())
        self.assertEqual(form.cleaned_data['table_name'], 'lowcode_product')

    def test_duplicate_model_name(self):
        data = {'name': 'UserGroup', 'table_name': '', 'fields': '[{"name":"x","type":"CharField","kwargs":{"max_length":10}}]'}
        form = ModelLowCodeForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn('模型名称"UserGroup"已存在', str(form.errors))

    def test_edit_existing_model_with_same_name(self):
        data = {
            'name': 'UserGroup',  # 与自身相同，应允许
            'table_name': 'lowcode_usergroup',
            'fields': '[{"name":"title","type":"CharField","kwargs":{"max_length":150}}]'
        }
        form = ModelLowCodeForm(data=data, instance=self.existing)
        self.assertTrue(form.is_valid())

    def test_invalid_field_config_missing_max_length(self):
        data = {
            'name': 'TestModel',
            'fields': '[{"name": "name", "type": "CharField", "kwargs": {}}]'
        }
        form = ModelLowCodeForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn('必须指定 max_length', str(form.errors))

    def test_invalid_json_format(self):
        data = {
            'name': 'TestModel',
            'fields': '{"not": "an array"}'
        }
        form = ModelLowCodeForm(data=data)
        self.assertFalse(form.is_valid())
        self.assertIn('必须是JSON数组格式', str(form.errors))