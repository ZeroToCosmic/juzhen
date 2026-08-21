# Frontend monorepo
## Current status: not implemented
当前为单npm项目、Jinja和原生JS。
## Current substitute
`gateway/templates`、`gateway/static`、共享sidebar/shell文件。
## Operational impact
依赖和页面边界不独立，接口类型靠人工同步。
## Preconditions for future work
确定框架、Node版本、包边界、构建/发布、Legacy页面共存和迁移顺序。
## Decision required before implementation
是否引入pnpm/TypeScript/React及是否继续由Flask托管静态产物。
## Evidence
`package.json`、`package-lock.json`；无`pnpm-workspace.yaml`。
