# LowCode/engine.py
from django.db import models


def get_dynamic_model(model_id):
    from .models import ModelLowCode
    try:
        modellowcode = ModelLowCode.objects.get(id=model_id)
        model_name = modellowcode.name
        table_name = modellowcode.table_name
        fields_lowcode = modellowcode.fields

        dynamic_fields = {}
        for field in fields_lowcode:
            field_name = field["name"]
            field_type = field["type"]
            verbose_name = field.get("verbose_name", field_name)

            if field_type == "CharField":
                max_length = field.get("max_length", 100)
                dynamic_fields[field_name] = models.CharField(
                    verbose_name=verbose_name,
                    max_length=max_length,
                    null=field.get("null", False),
                    blank=field.get("blank", False)
                )
            elif field_type == "TextField":
                dynamic_fields[field_name] = models.TextField(
                    verbose_name=verbose_name,
                    null=field.get("null", False),
                    blank=field.get("blank", False)
                )

        class DynamicModelMeta:
            db_table = table_name
            verbose_name = model_name
            verbose_name_plural = f"{model_name}列表"
            app_label = "LowCode"  # 必须与 settings 中应用名一致

        DynamicModel = type(
            model_name,  # 例如 "UserGroup"，最终类名为 UserGroup
            (models.Model,),
            {**dynamic_fields, "Meta": DynamicModelMeta, "__module__": "LowCode.models"}
        )

        return DynamicModel
    except ModelLowCode.DoesNotExist:
        return None