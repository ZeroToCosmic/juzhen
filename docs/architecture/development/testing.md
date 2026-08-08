# 测试

Python：`python -m pytest <target> -q -p no:cacheprovider`。Node：`npm run test:node`或`node --test tests-js/<file>`。

模块最低覆盖：Route合同、Schema严格性、Service规则、Store事务回滚、revision竞争、恢复、脱敏。浏览器测试使用Fake Page/Adapter/Redis并安装network/submit/click bomb。真实AdsPower只做用户批准的受控验收，报告必须写明Profile掩码、未执行项和实际结果。

当前Windows完整混合套件存在既有`session_key.py`权限/句柄问题；不能用局部绿测宣称全量通过。
