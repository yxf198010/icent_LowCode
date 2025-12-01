# lowcode/management/commands/backup_db.py
# 1. 创建备份（默认到 backups/ 目录）
# Bash
# 编辑
# python manage.py backup_db
# 2. 指定输出目录
# Bash
# 编辑
# python manage.py backup_db --output-dir /opt/backups/myapp
# 3. 自动化（如 cron）
# Cron
# 编辑
# # 每天凌晨 2 点备份
# 0 2 * * * cd /path/to/project && python manage.py backup_db --output-dir /data/backups
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
from django.db import connection


class Command(BaseCommand):
    help = "Backup the database (supports SQLite, PostgreSQL, MySQL)"

    def add_arguments(self, parser):
        parser.add_argument(
            "--output-dir",
            type=str,
            default=None,
            help="Directory to save the backup file (default: <project_root>/backups/)",
        )

    def handle(self, *args, **options):
        output_dir = options["output_dir"]
        if output_dir is None:
            output_dir = Path(settings.BASE_DIR) / "backups"
        else:
            output_dir = Path(output_dir)

        output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        vendor = connection.vendor

        try:
            if vendor == "sqlite":
                self._backup_sqlite(output_dir, timestamp)
            elif vendor == "postgresql":
                self._backup_postgresql(output_dir, timestamp)
            elif vendor in ("mysql", "mariadb"):
                self._backup_mysql(output_dir, timestamp)
            else:
                raise CommandError(f"Unsupported database vendor: {vendor}")
        except Exception as e:
            raise CommandError(f"Backup failed: {e}") from e

    def _backup_sqlite(self, output_dir: Path, timestamp: str):
        db_path = settings.DATABASES["default"]["NAME"]
        if not db_path or not os.path.isfile(db_path):
            raise CommandError("SQLite database file not found.")

        backup_path = output_dir / f"backup_{timestamp}.sqlite3"
        shutil.copy2(db_path, backup_path)
        self.stdout.write(
            self.style.SUCCESS(f"✅ SQLite backup completed: {backup_path}")
        )

    def _backup_postgresql(self, output_dir: Path, timestamp: str):
        db_config = settings.DATABASES["default"]
        required_keys = ["NAME", "USER", "HOST", "PORT"]
        for key in required_keys:
            if not db_config.get(key):
                raise CommandError(f"Missing DATABASES['default']['{key}'] setting")

        backup_path = output_dir / f"backup_{timestamp}.sql"
        cmd = [
            "pg_dump",
            "-h", db_config["HOST"],
            "-p", str(db_config["PORT"]),
            "-U", db_config["USER"],
            "-d", db_config["NAME"],
            "-f", str(backup_path),
        ]

        env = os.environ.copy()
        if db_config.get("PASSWORD"):
            env["PGPASSWORD"] = db_config["PASSWORD"]

        try:
            subprocess.run(cmd, env=env, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            raise CommandError(f"pg_dump failed: {e.stderr}") from e

        self.stdout.write(
            self.style.SUCCESS(f"✅ PostgreSQL backup completed: {backup_path}")
        )

    def _backup_mysql(self, output_dir: Path, timestamp: str):
        db_config = settings.DATABASES["default"]
        required_keys = ["NAME", "HOST", "PORT"]
        for key in required_keys:
            if not db_config.get(key):
                raise CommandError(f"Missing DATABASES['default']['{key}'] setting")

        backup_path = output_dir / f"backup_{timestamp}.sql"

        # 构建 mysqldump 命令（避免 shell=True 安全风险）
        cmd = [
            "mysqldump",
            "-h", db_config["HOST"],
            "-P", str(db_config["PORT"]),
            db_config["NAME"],
        ]

        if db_config.get("USER"):
            cmd += ["-u", db_config["USER"]]

        # 使用 --defaults-extra-file 避免密码暴露在命令行（更安全）
        # 但为简化，这里使用环境变量 + stdin（或临时文件）
        # 更佳实践：要求用户配置 .my.cnf，但此处兼容性优先

        env = os.environ.copy()
        password = db_config.get("PASSWORD")
        if password:
            # 通过环境变量传递密码（部分系统支持 MYSQL_PWD）
            env["MYSQL_PWD"] = password

        try:
            with open(backup_path, "w") as f:
                subprocess.run(cmd, env=env, stdout=f, check=True, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError as e:
            raise CommandError(f"mysqldump failed: {e.stderr.decode()}") from e

        self.stdout.write(
            self.style.SUCCESS(f"✅ MySQL backup completed: {backup_path}")
        )