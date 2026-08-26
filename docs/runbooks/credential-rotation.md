# 交易凭证轮换

Polymarket Google/邮箱账户只能通过 [官方 Magic key 导出流程](https://help.polymarket.com/en/articles/13364258-how-do-i-export-my-key) 获取 signer 私钥。不要提交 Google 密码、OAuth token、浏览器 cookie；账户类型使用 `magic_proxy`，proxy/funder 必须与公开 profile 一致。

1. 先关闭数据库开仓开关，等待已提交执行完成恢复或进入异常处置。
2. 在平台创建最小权限的新凭证；API、前端、discovery 和 market-data 进程不得获得交易凭证。
3. 将新凭证写入部署密钥库，只重启 execution worker。不得写入 `.env`、数据库、审计 payload 或日志。
4. 使用“测试连接（不会下单）”读取余额、allowance、开放订单和近期成交，确认 owner/proxy/funder/signature；不得把真实下单作为凭证轮换验收步骤。
5. 撤销旧凭证，检查旧凭证拒绝指标和审计记录。
6. 完成两端余额、开放订单和近期成交全量对账后，按 kill switch runbook 恢复。

任何日志或错误响应出现密钥、签名、cookie 或 bearer token 时，视为凭证泄露并立即重新轮换。
