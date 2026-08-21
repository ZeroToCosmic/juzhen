# Root Docker Compose
## Current status: not implemented
根项目没有统一Compose。
## Current substitute
Windows Launcher；仅TikTok API子服务有Compose。
## Operational impact
Redis/MySQL/Flask依赖需本机分别准备，环境复现成本高。
## Preconditions for future work
区分可容器化控制面和必须留在Windows桌面的AdsPower执行器。
## Decision required before implementation
明确哪些服务进入Compose、数据卷、端口、秘密管理和Windows互联方式。
## Evidence
仅存在`services/tiktok_api/docker-compose.yml`。
