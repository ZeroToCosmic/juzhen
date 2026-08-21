# 拟人化动作

五类V2动作：鼠标移动、视频切换/滚动、点击、键盘输入、等待。鼠标复用`ghost-cursor@1.4.2`→Node Worker→Python Bridge→`human_move_to()`；输入复用`actions_dom.human_type()`，不重复实现第二套轨迹/节奏。

输入前清空目标，逐字输入后从value/textContent读取并按同一规范精确比较冻结文案。视频切换当前优先使用ArrowDown；wheel calibration保留为诊断能力。每个动作按策略数组顺序执行，不重放已完成动作。
