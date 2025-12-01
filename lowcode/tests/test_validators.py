# tests/test_validators.py
from django.test import TestCase
from django.core.exceptions import ValidationError
from your_app.validators import (
    validate_model_name,
    validate_table_name_format,
    validate_field_config_json,
    validate_each_field
)


class ValidatorsTest(TestCase):
    def test_valid_model_name(self):
        try:
            validate_model_name("UserProfile")
        except ValidationError:
            self.fail("Valid model name raised ValidationError")

    def test_invalid_model_name_lowercase(self):
        with self.assertRaises(ValidationError):
            validate_model_name("userProfile")

    def test_python_keyword_rejected(self):
        with self.assertRaises(ValidationError):
            validate_model_name("Class")

    def test_valid_table_name(self):
        try:
            validate_table_name_format("lowcode_order")
        except ValidationError:
            self.fail("Valid table name raised error")

    def test_invalid_table_name_start_with_number(self):
        with self.assertRaises(ValidationError):
            validate_table_name_format("123table")

    def test_valid_field_config_json(self):
        raw = '[{"name":"title","type":"CharField","kwargs":{"max_length":100}}]'
        result = validate_field_config_json(raw)
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)

    def test_empty_field_config(self):
        with self.assertRaises(ValidationError):
            validate_field_config_json("")

    def test_field_validation_success(self):
        fields = [
            {"name": "email", "type": "EmailField", "kwargs": {}},
            {"name": "active", "type": "BooleanField", "kwargs": {"default": True}}
        ]
        try:
            validate_each_field(fields)
        except ValidationError:
            self.fail("Valid field config raised error")

    def test_duplicate_field_name(self):
        fields = [
            {"name": "title", "type": "CharField", "kwargs": {"max_length": 50}},
            {"name": "title", "type": "TextField", "kwargs": {}}
        ]
        with self.assertRaises(ValidationError) as cm:
            validate_each_field(fields)
        self.assertIn('重复', str(cm.exception))