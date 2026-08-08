# 元素定位

V2元素由真实页面人工点选。定义保存多组CSS/XPath、frame path、用途、kind、诊断和revision。运行时逐候选验证：匹配恰好一个节点、可见、边界非零；input还必须可编辑。禁止`locator.first()`、绝对XPath、纯坐标或歧义猜测。

评论输入兼容`input`、`textarea`、`role=textbox`和`contenteditable`。线程回复先唯一定位父评论，再限定到“最近comment container等于当前父节点”的直接Reply/composer控件；子Receipt必须绑定父scope。
