# 服务边界

推荐依赖方向：Route/Blueprint→Pydantic或显式输入验证→Service/Domain→Store/Adapter→外部系统。Route不直接写SQL；Store不调用浏览器；Adapter不返回raw凭据给公共层；Executor不定义HTTP响应。

当前较清晰模块：`execution_v2`、`comment_campaign`、`tiktok_stats`。`selector_probe/blueprint.py`和`gateway/app.py`仍含较多编排，是Legacy例外。新增功能不能继续扩大例外。
