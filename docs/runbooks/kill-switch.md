# Kill Switch 操作

## 关闭开仓

1. 本机 operator 调用 `PUT /api/system-control/opening`，提交
   `{"enabled": false, "reason": "..."}`。
2. 确认响应与数据库 `system_control.opening_enabled` 均为 `false`。
3. 检查 `SUBMITTED`、`PARTIALLY_HEDGED` 和开放订单。关闭开关不会自动撤单或平仓。
4. 部分对冲按 `partially-hedged.md` 处置；对账异常按 `reconciliation-failure.md` 处置。

## 恢复开仓

持久化的“真实下单”开关是新开仓唯一的人工许可。关闭开关不影响发现、审核、行情读取或机会展示。
operator 在故障处置完成、记录事故原因并完成全量对账后，通过 Integrations 页面或
`PUT /api/system-control/opening` 开启。恢复不需要交易模式或自动化证据；不得修改数据库绕过 API。

人工负责系统级恢复审核，不逐笔批准执行授权或订单。
