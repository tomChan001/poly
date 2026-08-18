# Kill Switch 操作

## 关闭开仓

1. operator 使用已验证 OIDC 会话调用 `PUT /api/system-control/opening`，提交
   `{"enabled": false, "reason": "..."}`。
2. 确认响应与数据库 `system_control.opening_enabled` 均为 `false`。
3. 检查 `SUBMITTED`、`PARTIALLY_HEDGED` 和开放订单。关闭开关不会自动撤单或平仓。
4. 部分对冲按 `partially-hedged.md` 处置；对账异常按 `reconciliation-failure.md` 处置。

## 恢复开仓

恢复必须同时满足部署环境 `TRADING_MODE=limited_auto`、自动化证据门槛通过和数据库开关开启。
operator 记录事故原因、修复证据和全量对账结果后才可请求开启。系统返回的任一门槛拒绝原因都必须处理，
不得修改数据库绕过 API。

人工负责系统级恢复审核，不逐笔批准执行授权或订单。
