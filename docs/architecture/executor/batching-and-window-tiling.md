# 批次与窗口平铺

默认每批3个Profile；系统支持数百Profile。每批先取得全部需要的profile lease，再start。窗口按当前可用视口等分：2窗各二分之一，3窗各三分之一。

批内单Profile失败不阻塞其他Profile；所有已start Profile都进入finally关闭。只有整批每个Profile stop且is_active=false，才分配下一generation。任何关闭不确认：Campaign暂停、Profile健康置unhealthy/disabled、保留隔离语义，不开下一批。
