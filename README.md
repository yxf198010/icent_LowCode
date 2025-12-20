## 数据库配置（生产环境）

推荐通过环境变量配置 PostgreSQL 连接信息：

```bash
export PG_HOST="your-db-host"
export PG_DBNAME="your_db"
export PG_USER="your_user"
export PG_PASSWORD="your_secure_password"
export PG_PORT="5432"
export PG_SSLMODE="require"

### 动态模型配置
1. 复制 `dynamic_models.example.json` 为 `dynamic_models.json`
2. 根据业务需求修改 `dynamic_models.json`（该文件不会被 git 跟踪）
3. 若新增模型字段，同步更新示例模板文件 `dynamic_models.example.json`