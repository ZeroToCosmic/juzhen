# Docker

当前只有`services/tiktok_api/docker-compose.yml`。它构建/运行固定上游TikTok API，绑定`127.0.0.1:53281`并只读挂载配置。

```powershell
powershell -ExecutionPolicy Bypass -File scripts/install_tiktok_api.ps1
powershell -ExecutionPolicy Bypass -File scripts/start_tiktok_api.ps1
```

根项目没有Flask、Redis、MySQL统一Compose。Windows AdsPower执行器依赖桌面和本机程序，不适合直接容器化。不要运行`docker compose up`并假设整个系统会启动。
