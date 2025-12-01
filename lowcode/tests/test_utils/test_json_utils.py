# tests/test_utils/test_json_utils.py
from django.test import TestCase
from your_app.utils.json_utils import parse_json_array, format_json_for_storage


class JsonUtilsTest(TestCase):
    def test_parse_valid_json_array(self):
        raw = '[{"name":"x"}]'
        result = parse_json_array(raw)
        self.assertEqual(result, [{"name": "x"}])

    def test_parse_non_array_raises(self):
        with self.assertRaises(TypeError):
            parse_json_array('{"not": "array"}')

    def test_parse_empty_string_raises(self):
        with self.assertRaises(ValueError):
            parse_json_array("")

    def test_format_json(self):
        data = [{"name": "title", "type": "CharField"}]
        formatted = format_json_for_storage(data)
        self.assertIn('"name": "title"', formatted)
        self.assertTrue(formatted.startswith('[\n  {'))