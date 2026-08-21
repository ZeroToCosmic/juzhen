# AdsPower Adapter

`adspower.py::AdsPowerController`负责列表分页、start、stop、active查询；V2通过`RateLimitedAdsPowerAdapter`限制调用。完整Profile分页使用原始page count/total判断，并设置5000条硬上限，避免过滤后提前停止或重复满页无限循环。

Adapter内部接收raw ID；公共层只接收token/profile_ref。响应白名单投影`id/name/status`后才能进入Campaign身份同步，禁止把`group_name`等未声明字段直传严格Store。
