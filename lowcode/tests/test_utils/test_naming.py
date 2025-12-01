# tests/test_utils/test_naming.py
from django.test import TestCase
from your_app.utils.naming import (
    generate_table_name_from_model,
    is_valid_python_class_name,
    is_valid_db_table_name,
    is_valid_field_name
)


class NamingUtilsTest(TestCase):
    def test_generate_table_name(self):
        self.assertEqual(generate_table_name_from_model("OrderItem"), "lowcode_orderitem")

    def test_valid_class_name(self):
        self.assertTrue(is_valid_python_class_name("MyModel"))
        self.assertFalse(is_valid_python_class_name("myModel"))
        self.assertFalse(is_valid_python_class_name("123Model"))

    def test_valid_table_name(self):
        self.assertTrue(is_valid_db_table_name("lowcode_user"))
        self.assertTrue(is_valid_db_table_name("_temp_table"))
        self.assertFalse(is_valid_db_table_name("123table"))
        self.assertFalse(is_valid_db_table_name("user-table"))

    def test_valid_field_name(self):
        self.assertTrue(is_valid_field_name("user_name"))
        self.assertFalse(is_valid_field_name("UserName"))
        self.assertFalse(is_valid_field_name("123field"))