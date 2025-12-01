# 在 test_model_storage.py 中添加
def test_auto_create_table(self):
    from lowcode.engine import get_dynamic_model_by_config
    from lowcode.db_utils import create_table_for_model, table_exists

    fields = [{"name": "test_field", "type": "CharField", "max_length": 50}]
    model_cls = get_dynamic_model_by_config("TestAutoTable", fields, "lowcode_testautotable")

    self.assertTrue(create_table_for_model(model_cls))
    self.assertTrue(table_exists("lowcode_testautotable"))