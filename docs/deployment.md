## 数据库配置（生产环境）

推荐通过环境变量配置 PostgreSQL 连接信息：

```bash
export PG_HOST="your-db-host"
export PG_DBNAME="your_db"
export PG_USER="your_user"
export PG_PASSWORD="your_secure_password"
export PG_PORT="5432"
export PG_SSLMODE="require"