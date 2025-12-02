动态模型升级 API
本系统提供安全、异步的 Web API，用于远程升级低代码动态模型结构（字段变更）。支持自动备份、兼容性检查与服务重启。

🔐 权限要求：仅限 Django 管理员用户（is_staff=True）通过 Token 或 Session 访问。

📡 接口概览
方法	路径	说明
POST	/lowcode/api/upgrade-model/	触发模型升级任务
GET	/lowcode/api/upgrade-status/{task_id}/	查询任务执行状态
1️⃣ 触发模型升级
请求示例
Bash
编辑
curl -X POST http://your-domain.com/lowcode/api/upgrade-model/ \
  -H "Authorization: Token YOUR_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model_name": "Product",
    "fields": [
      {"name": "name", "type": "CharField", "max_length": 100},
      {"name": "description", "type": "TextField", "null": true},
      {"name": "price", "type": "FloatField"}
    ],
    "no_backup": false,
    "no_restart": false,
    "force": false
  }'
请求参数
字段	类型	必填	默认值	说明
model_name	string	✅	—	模型名称（字母、数字、下划线）
fields	array	✅	—	新的完整字段定义列表
no_backup	boolean	❌	false	跳过数据库备份（⚠️ 不推荐）
no_restart	boolean	❌	false	升级后不重启 Django 服务
force	boolean	❌	false	跳过字段兼容性检查（💥 高危操作）
💡 字段类型支持：CharField, TextField, IntegerField, FloatField, BooleanField, DateTimeField 等（详见 FIELD_METADATA）。

成功响应（HTTP 202）
Json
编辑
{
  "task_id": "a1b2c3d4-e5f6-7890-g1h2-i3j4k5l6m7n8",
  "message": "升级任务已启动",
  "status_check_url": "/lowcode/api/upgrade-status/a1b2c3d4-e5f6-7890-g1h2-i3j4k5l6m7n8/"
}
2️⃣ 查询任务状态
请求示例
Bash
编辑
curl -H "Authorization: Token YOUR_ADMIN_TOKEN" \
  http://your-domain.com/lowcode/api/upgrade-status/a1b2c3d4-e5f6-7890-g1h2-i3j4k5l6m7n8/
响应示例
运行中：
Json
编辑
{ "status": "running" }
成功：
Json
编辑
{
  "status": "success",
  "message": "模型 Product 升级成功"
}
失败：
Json
编辑
{
  "status": "failed",
  "error": "字段 'age' 类型从 CharField 改为 IntegerField 可能导致数据丢失"
}
未找到：
Json
编辑
{ "status": "not_found" }
⚠️ 安全与最佳实践
永远不要在生产环境使用 force=true，除非你完全理解数据丢失风险。
确保数据库备份机制可用（SQLite 自动复制；PostgreSQL/MySQL 需安装 pg_dump / mysqldump）。
建议先在测试环境验证字段变更。
升级后务必验证业务功能是否正常。
🛠️ 故障恢复
若升级失败：

查看返回的错误信息；
使用备份文件手动恢复数据库（位于 backups/ 目录）；
修复字段配置后重试。
💾 备份文件命名格式：backup_YYYYMMDD_HHMMSS.sqlite3（或 .sql）

🔧 依赖说明
认证：Django REST Framework + TokenAuthentication 或 Session
异步：Celery（推荐）或内置线程（开发环境）
数据库：SQLite / PostgreSQL / MySQL（需对应 CLI 工具备份）