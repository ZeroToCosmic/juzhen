# 业务控制系统基础设施（M0 原型）

v1 起步形态：单节点 PostgreSQL / Redis / NATS（JetStream），本机或主控机运行。

```powershell
docker compose -f infra/docker-compose.yml up -d
docker compose -f infra/docker-compose.yml ps   # 健康检查
```

端口：PG 5432、Redis 6379、NATS 4222（JetStream 监控 8222）。

## 生产形态（M3+，按 ADR-0010 决策）

- nats-server 3 节点集群（JetStream 持久化需 ≥3 节点），启用 TLS + token/认证
- PostgreSQL 主从；Redis 主从（AOF）
- 凭据全部环境变量注入，禁止使用本文件默认口令
- 参考端口矩阵：`docs/PRD-业务控制系统.v2.md` §13

## 与现有系统的关系

- 现有 Redis（`local-redis` 容器或本机）与中控 Redis 在迁移期可共用或分库（`REDIS_DB` 隔离）
- 现有 RQ 队列在 M2 引入 NATS 前继续承担 Comment Campaign 投递
- 本 Compose 仅承载**中控侧**基础设施；Agent 侧（Windows）不使用 Docker
