# 执行器安全边界

- 探针只读动作禁止input/submit/like/follow/publish。
- Campaign提交需要durable approval、exact revision、Campaign running、证据未变、租约仍由当前owner持有。
- 定位submit控件并最终refresh租约后，CAS到submitting，再立即只click一次。
- 点击后异常全部进入unverified，不自动重试。
- raw Profile ID、ws、Cookie、认证头只在最小内部边界出现。
- Evidence只用UUID PNG相对路径；路径投影拒绝穿越、反斜杠、大小写扩展和symlink。
- 自动测试使用Fake Page/Adapter/Redis，并安装真实网络和submit/click tripwire。
