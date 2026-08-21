# 并行开发

目录主要所有者：Gateway、V2、Probe、Campaign、Stats、Node Browser、Launcher、Architecture Docs。跨模块先冻结接口、ID、错误码、消息、表/状态，再分别实现。

避免多人同时修改`gateway/app.py`同一区域。每个任务声明Files/Consumes/Produces；实现后进行架构复核和功能复核。共享工作区中不得reset/checkout他人改动；冲突无法绕开时停止并协调。
