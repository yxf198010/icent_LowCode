# tests/test_utils/test_model_utils.py
from django.test import TestCase
from your_app.models import ModelLowCode
from your_app.utils.model_utils import (
    is_model_name_unique,
    is_table_name_unique,
    ensure_unique_table_name
)


class ModelUtilsTest(TestCase):
    def setUp(self):
        ModelLowCode.objects.create(name="Existing", table_name="lowcode_existing")

    def test_is_model_name_unique(self):
        self.assertTrue(is_model_name_unique("NewModel"))
        self.assertFalse(is_model_name_unique("Existing"))

    def test_is_model_name_unique_exclude_self(self):
        obj = ModelLowCode.objects.get(name="Existing")
        self.assertTrue(is_model_name_unique("Existing", exclude_id=obj.id))

    def test_ensure_unique_table_name(self):
        base = "lowcode_test"
        # 第一次应直接返回
        self.assertEqual(ensure_unique_table_name(base), base)
        # 创建冲突
        ModelLowCode.objects.create(name="Test1", table_name=base)
        # 应返回带后缀
        self.assertEqual(ensure_unique_table_name(base), f"{base}_1")