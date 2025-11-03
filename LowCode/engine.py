from django.apps import apps
from django.db import models
from LowCode.models import ModelConfig


def generate_model(Icent_LowCode: ModelConfig):
    """根据配置动态生成Django模型类"""
    # 构建模型字段
    fields = {
        # 内置主键字段
        "id": models.AutoField(primary_key=True, verbose_name="ID"),
        "create_time": models.DateTimeField(auto_now_add=True, verbose_name="创建时间"),
        "update_time": models.DateTimeField(auto_now=True, verbose_name="更新时间"),
    }

    # 解析JSON配置的自定义字段
    for field_conf in Icent_LowCode.fields:
        field_name = field_conf["name"]
        field_type = field_conf["type"]
        # 排除name和type，其余作为字段参数（如max_length、verbose_name）
        field_kwargs = {k: v for k, v in field_conf.items() if k not in ("name", "type")}

        # 映射字段类型（可扩展更多字段）
        if field_type == "CharField":
            # 给CharField设置默认max_length（防止配置遗漏）
            field_kwargs.setdefault("max_length", 255)
            fields[field_name] = models.CharField(**field_kwargs)
        elif field_type == "IntegerField":
            fields[field_name] = models.IntegerField(**field_kwargs)
        elif field_type == "TextField":
            fields[field_name] = models.TextField(**field_kwargs)
        elif field_type == "DateField":
            fields[field_name] = models.DateField(**field_kwargs, null=True, blank=True)

    # 动态创建模型类
    model_class = type(
        Icent_LowCode.name,  # 模型类名（如Article）
        (models.Model,),  # 继承自Django Model
        {
            **fields,
            "__module__": "config_app.dynamic_models",  # 虚拟模块名
            "__tablename__": Icent_LowCode.table_name,  # 数据库表名
            "Meta": type("Meta", (), {"verbose_name": Icent_LowCode.name, "verbose_name_plural": Icent_LowCode.name + "列表"})
        }
    )

    # 注册模型到Django应用（关键步骤）
    apps.register_model("config_app", model_class)
    return model_class


def get_dynamic_model(model_id):
    """通过模型ID获取动态生成的模型类"""
    config = ModelConfig.objects.get(id=model_id)
    return generate_model(config)