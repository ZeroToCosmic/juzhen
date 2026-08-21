# 本机开发环境

## 前提
Windows、AdsPower、Python、Node.js、Redis；MySQL仅旧ORM模式需要；Docker仅TikTok API需要。

## 安装
```powershell
python -m pip install -r requirements.txt
npm install
powershell -ExecutionPolicy Bypass -File scripts/install_tiktok_api.ps1
```

复制`.env`/`config.example.json`时只填本机值，不提交真实`config.json`。运行`python launcher.py`或`start_console.vbs`。首次启动先查看启动器依赖检查，不直接执行真实策略。

## 基础验证
```powershell
python -m pytest tests/test_adspower.py tests/test_execution_v2_store.py -q
npm run test:node
```
具体模块命令见开发测试文档。AdsPower/TikTok真实验收需要单独人工授权。
