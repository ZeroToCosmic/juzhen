# Browser V2 默认随机源修复设计

## 问题

`StrategyExecutor` 的 `rng` 默认值为 `None`，但执行动作时仍显式传入 `rng=None`。这覆盖了 `execute_action` 自身的默认随机源。等待动作调用 `_sample()` 后执行 `rng.uniform()`，产生：

```text
'NoneType' object has no attribute 'uniform'
```

最新任务因此在第一个等待动作失败，后续滚动尚未执行。

## 方案

采用方案 A：在 `StrategyExecutor.__init__` 中归一化随机源。

```python
self._rng = rng if rng is not None else random
```

`executor.py` 增加标准库 `random` 导入。显式注入的随机源保持原对象，不改变现有确定性测试和运行时注入能力。生产未注入时使用 Python 标准库随机模块。

## 范围

修改：

- `execution_v2/executor.py`
- `tests/test_execution_v2_executor.py`

不修改：

- 动作参数与策略格式
- `execution_v2/actions.py`
- UI、API、SQLite、AdsPower 接线
- 拟人化鼠标与键盘实现

## 测试

增加使用真实 `execute_action` 的 Executor 回归测试：

1. 不注入 `rng`。
2. 策略只包含一个 `wait` 动作，范围为 `[0, 0]`。
3. 注入无等待的 `sleep`，避免测试延迟。
4. 断言任务成功、动作成功、`duration_seconds == 0`。

保留现有注入随机源测试，确认自定义随机源不被替换。

## 成功标准

- 生产默认 Executor 执行等待动作不再产生 `NoneType.uniform`。
- 等待、滚动、点击、移动、输入间隔均收到有效随机源。
- 既有 Executor 和动作测试全部通过。
