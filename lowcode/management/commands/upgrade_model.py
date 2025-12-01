# lowcode/management/commands/upgrade_model.py
"""
Django 管理命令：通过 CLI 启动低代码模型升级任务。

该命令与 API 行为完全一致，复用 UpgradeModelSerializer 进行参数校验，
确保命令行与 Web 接口逻辑统一、安全可靠。
"""

import json
import logging
from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth import get_user_model
from rest_framework.exceptions import ValidationError

from lowcode.api.serializers import UpgradeModelSerializer
from lowcode.api.views import TASK_BACKEND, async_upgrade_model_task

logger = logging.getLogger(__name__)
User = get_user_model()


class Command(BaseCommand):
    help = "通过命令行启动低代码模型升级任务（与 API 行为一致）"

    def add_arguments(self, parser):
        parser.add_argument(
            '--model-name',
            type=str,
            required=True,
            help="目标模型名称（如 'UserProfile'）"
        )
        parser.add_argument(
            '--fields',
            type=str,
            required=True,
            help='JSON 格式的字段定义列表，例如：\'[{"name":"age","type":"int"}]\''
        )
        parser.add_argument(
            '--user-id',
            type=int,
            default=None,
            help="操作用户 ID（用于审计记录，可选）"
        )
        parser.add_argument(
            '--no-backup',
            action='store_true',
            help="跳过数据库备份（危险操作！）"
        )
        parser.add_argument(
            '--no-restart',
            action='store_true',
            help="跳过服务重启步骤"
        )
        parser.add_argument(
            '--force',
            action='store_true',
            help="强制执行（忽略字段冲突等警告）"
        )

        # 在 help 末尾添加使用示例
        parser.epilog = """
示例：
  python manage.py upgrade_model \\
    --model-name Product \\
    --fields '[{"name": "price", "type": "decimal", "max_digits": 10, "decimal_places": 2}]' \\
    --user-id 1 \\
    --force
"""

    def handle(self, *args, **options):
        # === 1. 解析 fields 参数 ===
        try:
            fields = json.loads(options['fields'])
            if not isinstance(fields, list):
                raise ValueError("fields 必须是一个 JSON 数组")
        except (json.JSONDecodeError, ValueError) as e:
            raise CommandError(f"❌ --fields 参数解析失败: {e}")

        # === 2. 获取操作用户（如果提供）===
        user = None
        if options['user_id'] is not None:
            try:
                user = User.objects.get(pk=options['user_id'])
            except User.DoesNotExist:
                raise CommandError(f"❌ 用户 ID {options['user_id']} 不存在")

        # === 3. 构造序列化器输入数据 ===
        data = {
            'model_name': options['model_name'],
            'fields': fields,
            'no_backup': options['no_backup'],
            'no_restart': options['no_restart'],
            'force': options['force'],
        }

        # === 4. 模拟 request 上下文（供 serializer 使用）===
        fake_request = type('FakeRequest', (), {'user': user})()

        # === 5. 使用序列化器进行完整校验 ===
        serializer = UpgradeModelSerializer(data=data, context={'request': fake_request})
        try:
            serializer.is_valid(raise_exception=True)
        except ValidationError as e:
            raise CommandError(f"❌ 参数校验失败: {e.detail}")

        validated_data = serializer.validated_data
        user_id = validated_data.get('user_id') or (user.id if user else None)

        # === 6. 启动异步任务 ===
        try:
            if TASK_BACKEND == 'celery':
                task = async_upgrade_model_task.delay(
                    model_name=validated_data['model_name'],
                    fields=validated_data['fields'],
                    user_id=user_id,
                    no_backup=validated_data['no_backup'],
                    no_restart=validated_data['no_restart'],
                    force=validated_data['force']
                )
                task_id = task.id
            else:  # thread backend
                task_id = async_upgrade_model_task(
                    model_name=validated_data['model_name'],
                    fields=validated_data['fields'],
                    user_id=user_id,
                    no_backup=validated_data['no_backup'],
                    no_restart=validated_data['no_restart'],
                    force=validated_data['force']
                )

            self.stdout.write(
                self.style.SUCCESS(
                    f"✅ 升级任务已成功启动！\n"
                    f"   • Task ID: {task_id}\n"
                    f"   • Backend: {TASK_BACKEND}\n"
                    f"   • Model: {validated_data['model_name']}"
                )
            )
            logger.info(
                f"CLI upgrade task started: task_id={task_id}, "
                f"model={validated_data['model_name']}, user_id={user_id}"
            )

        except Exception as e:
            logger.error(f"CLI 启动升级任务失败: {e}", exc_info=True)
            raise CommandError(f"❌ 任务启动异常: {e}")