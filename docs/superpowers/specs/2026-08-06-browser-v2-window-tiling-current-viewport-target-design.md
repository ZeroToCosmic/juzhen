# Browser V2 窗口平铺与当前视口目标设计

## 问题

Browser V2 批次调度器只负责启动 AdsPower Profile、连接 CDP、执行策略和关闭窗口，没有调用旧版窗口平铺器。因此同批窗口不会按屏幕工作区等面积排列。

最新真实任务 `19a98d1f-77fe-4227-9e37-36b07bcb8907` 并非没有进入策略：两个 Profile 的首个等待动作分别成功执行约 17.8 秒和 19.0 秒。第二个“移动到视频播放容器”动作均因 `element_outside_viewport` 失败，安全停止规则随后阻止了滚动、点击和输入动作。保存的元素路径绑定了旧视频序号，无法代表当前屏幕正在播放的视频。

## 范围

- V2 每批窗口连接 CDP 后复用旧版 `window_tiler.tile_browser_windows()`。
- 动作元素的固定路径离开视口后，允许从其稳定属性链定位当前视口唯一对应元素。
- 保留现有批次关闭、失败即停、证据截图和 Profile 隔离规则。
- 不修改策略 schema、元素 schema、数据库表、旧版 Gateway 路由或旧版平铺算法。

## 组件一：批次窗口平铺

### 接入位置

`BatchScheduler` 在 `_start_and_bind_batch()` 完成后、`_execute_batch()` 开始前执行一次批次平铺。平铺器通过构造函数依赖注入，调度器不直接导入 Gateway。

生产服务提供一个异步适配器：把本批 `BrowserBinding` 转为 `tile_browser_windows()` 需要的提示列表，每项只包含内部 Profile ID 和 `ws_puppeteer`；同步 Windows API 调用通过 `asyncio.to_thread()` 执行。

### 流程

1. 启动本批全部 Profile。
2. 为成功启动的 Profile 建立一对一 CDP 连接。
3. 把成功绑定的窗口作为同一批调用旧版平铺器。
4. 平铺通过后才导航策略目标网址并执行动作。
5. 无论平铺或动作成功失败，最终都关闭本批已启动窗口。
6. 只有全部关闭得到确认后才启动下一批。

### 布局

- 2 个窗口左右各占工作区 1/2。
- 3 个窗口各占工作区 1/3。
- 1–8 个窗口沿用旧版等面积、不重叠、置前一次和页面缩放逻辑。

### 阶段与失败

新增 `ProfileStatus.TILING` 和 `Stage.WINDOW_TILE`。管理页面把该阶段显示为“正在排列窗口”。

以下任一情况使本批平铺失败：

- 返回的平铺窗口数不等于成功绑定数；
- `missing` 非空；
- 任一布局项报告重叠；
- 任一页面缩放结果失败；
- 平铺器抛出异常。

失败 Profile 记录 `window_tile_failed`，不进入导航或动作执行，随后执行现有清理链路。错误摘要不得保存 WebSocket、原始 Profile ID 或其他敏感值。

## 组件二：当前视口目标解析

### 适用范围

只用于 `move`、`click` 和 `input` 动作。页面就绪元素继续使用当前严格唯一定位规则，不启用回退。

### 解析顺序

1. 按现有优先级验证保存的定位器。
2. 对动作目标增加视口要求：目标必须可见、未禁用、有面积，且中心点位于当前视口。
3. 如果保存的唯一目标位于视口外，或保存路径因固定视频序号失效，则从 CSS 路径提取稳定属性链。
4. 仅允许以下属性进入回退链：`data-e2e`、`data-testid`、`aria-label`、`name`、`placeholder`、`role`、`contenteditable`。
5. 忽略 `#one-column-item-N`、随机 class、绝对层级和文本内容；保留稳定属性的原有先后关系，构造后代 CSS 链。
6. 查询回退链，过滤为可见、未禁用、有面积、中心点位于当前视口的候选。
7. 恰好一个候选时返回；0 个返回 `current_viewport_target_not_found`；多个返回 `current_viewport_target_ambiguous`。

例如固定路径：

```text
#one-column-item-0 ... [data-e2e="feed-video"] ...
```

可回退为：

```text
[data-e2e="feed-video"]
```

评论按钮路径中的稳定属性保持为后代链：

```text
[data-e2e="comment-icon"] [data-testid="tux-web-icon-button-container"] [data-testid="tux-web-icon-button"]
```

这使旧元素记录能够指向当前屏幕中的同类控件，不需要重新点选，也不依赖语义匹配或 LLM。

## 安全边界

- 不使用元素文本、随机 class、绝对 XPath或固定视频序号作为当前视口回退依据。
- 不按相似度猜测；候选不是唯一时禁止动作。
- 不自动滚回保存元素所在的视频。
- 一个动作失败后停止该 Profile 后续动作，其他同批 Profile 继续各自执行。
- 平铺失败时禁止整批动作，避免错误窗口尺寸影响鼠标坐标。

## 验收

### 自动测试

- 调度器：平铺发生在 CDP 连接之后、动作之前。
- 调度器：2、3 个绑定以完整批次传给平铺器。
- 调度器：平铺失败时不调用执行器，并仍关闭全部已启动窗口。
- 调度器：上一批关闭确认早于下一批启动。
- 定位器：保存目标仍在视口时直接使用。
- 定位器：固定视频序号目标离开视口时解析当前视口唯一稳定属性目标。
- 定位器：0 个当前候选返回 `current_viewport_target_not_found`。
- 定位器：多个当前候选返回 `current_viewport_target_ambiguous`。
- 定位器：页面就绪路径不启用当前视口回退。
- 运行 Node 全量、V2 Python 全量和旧版 `test_window_tiler.py`。

### 真实验收

- 2 Profile 同批启动：两个窗口各占屏幕工作区 1/2；两个 Profile 均产生等待、移动、滚动动作记录。
- 3 Profile 同批启动：三个窗口各占工作区 1/3；窗口无重叠。
- 多批任务：上一批所有窗口关闭成功后才启动下一批。
- 任一定位失败时，历史记录显示准确动作序号、动作类型和错误码，并保存失败截图。
