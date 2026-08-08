# CI/CD
## Current status: not implemented
仓库没有`.github/workflows`或其他流水线定义。
## Current substitute
开发者本机运行pytest、node:test和手工启动器验收。
## Operational impact
全量测试、契约、乱码、秘密和文档链接没有自动门禁。
## Preconditions for future work
先解决Windows测试基线、确定Python/Node版本、Fake外部服务和Artifact保留。
## Decision required before implementation
选择GitHub Actions runner组合、分支保护、发布目标和真实验收隔离。
## Evidence
工作区文件清单无`.github/workflows`。
