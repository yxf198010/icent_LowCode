# lowcode/management/commands/delete_model.py
import re
from django.core.management.base import BaseCommand, CommandError

from lowcode.model_storage import delete_model_config, get_model_config
from lowcode.engine import get_dynamic_model_by_config
from lowcode.utils.db_utils import delete_table_for_model


def validate_model_name(name: str) -> str:
    """验证模型名称是否为合法的 Python 标识符"""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
        raise CommandError(
            "模型名称必须是有效的 Python 标识符（仅含字母、数字、下划线，且不以数字开头）"
        )
    return name


def generate_table_name(model_name: str) -> str:
    """根据模型名生成标准表名（与 create_model 命令保持一致）"""
    safe_name = re.sub(r'[^a-z0-9_]', '', model_name.lower())
    return f"lowcode_{safe_name}"[:63]


class Command(BaseCommand):
    help = '删除动态模型配置及对应的数据库表'

    def add_arguments(self, parser):
        parser.add_argument('model_name', type=str, help='要删除的模型名称')
        parser.add_argument('--force', action='store_true', help='跳过确认提示，强制删除')
        parser.add_argument('--no-db', action='store_true', help='仅删除模型配置，保留数据库表')

    def handle(self, *args, **options):
        raw_model_name = options['model_name']
        force = options['force']
        no_db = options['no_db']

        # 1. 验证模型名格式
        model_name = validate_model_name(raw_model_name)

        # 2. 获取配置
        config = get_model_config(model_name)
        if config is None:
            raise CommandError(f"❌ 模型 '{model_name}' 不存在")

        # 3. 用户确认（除非 --force）
        if not force:
            self.stdout.write(
                self.style.WARNING(f"⚠️ 即将删除模型 '{model_name}' 及其数据！")
            )
            confirm = input("确定继续吗？(y/N): ").strip()
            if confirm.lower() != 'y':
                self.stdout.write(self.style.NOTICE("🛑 操作已取消"))
                return

        table_deleted = False
        table_name = None

        # 4. 删除数据库表（除非 --no-db）
        if not no_db:
            # 优先使用配置中保存的 table_name，否则回退到标准命名
            table_name = config.get("table_name") or generate_table_name(model_name)

            try:
                # 构建动态模型类（用于 db_utils 识别表结构）
                DynamicModel = get_dynamic_model_by_config(
                    model_name=model_name,
                    fields=config["fields"],
                    table_name=table_name
                )
            except Exception as e:
                self.stdout.write(
                    self.style.ERROR(f"⚠️ 构建动态模型失败，跳过表删除: {e}")
                )
            else:
                try:
                    deleted = delete_table_for_model(DynamicModel)
                    if deleted:
                        table_deleted = True
                        self.stdout.write(
                            self.style.SUCCESS(f"✅ 数据库表已删除: {table_name}")
                        )
                    else:
                        self.stdout.write(
                            self.style.WARNING(f"⚠️ 数据库表删除失败或表不存在: {table_name}")
                        )
                except Exception as e:
                    self.stdout.write(
                        self.style.ERROR(f"❌ 删除数据库表时出错: {e}")
                    )

        # 5. 删除模型配置（关键步骤）
        try:
            success = delete_model_config(model_name)
            if success:
                self.stdout.write(
                    self.style.SUCCESS(f"✅ 模型配置已删除: {model_name}")
                )
            else:
                self.stdout.write(
                    self.style.ERROR("❌ 模型配置删除失败（存储层返回 False）")
                )
                # 注意：此时表可能已被删，但配置残留，需人工干预
                raise CommandError("模型配置删除失败")
        except Exception as e:
            self.stdout.write(
                self.style.ERROR(f"❌ 删除模型配置时发生异常: {e}")
            )
            raise CommandError(f"配置删除异常: {e}")

        # 6. 最终总结
        if not no_db and not table_deleted:
            self.stdout.write(
                self.style.WARNING("❗ 注意：数据库表可能未被删除，请手动检查清理")
            )

        self.stdout.write(self.style.SUCCESS("🗑️ 删除操作完成"))