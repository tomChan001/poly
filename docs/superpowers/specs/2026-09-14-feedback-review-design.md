# 用户反馈修复设计

用户授权一次性修复全部五项反馈，并由实现者选择推荐方案。保留工作区已有页面修改，在 codex/user-feedback-review 分支继续。

预审核以平台原生数据进行只读测算。默认显示满足当前风控的候选，允许查看全部候选及明确的未达标原因。每次刷新读取当前风控版本，不把历史合格状态当作当前结果。人工配对审核与经济条件筛选保持独立，测算不会授予真实下单许可。

新增 minimum_liquidity_contracts，单位为份，默认 1，含义为两边买入方向在当前最优卖价可成交量的较小值。执行和预审核共用此校验。ROI 使用现有深度扫描优化器，扣除费用、显式成本与风险缓冲；费用未知、订单簿不新鲜或结算时间不明确时不可达标。未知字段显示未提供，不能补造数值。

平台规则保留 Kalshi 主规则和补充规则、Polymarket 原生描述与结算来源。市场跳转与规则链接分开存储，旧数据仍可读取。规则变化触发重新审核。平台最晚结算日期优先于预计结算日期，缺少时间不能默认满足期限。

实现口径补充：预计最晚结算是计划持有期限，不保证争议后的最终到账时间。预审核检查本地订单簿接收时间，并检查所有已提供的平台采集时间；Kalshi REST 不提供采集时间时不伪造该字段，真实执行保留原有严格要求。原生费用先读取，随后读取订单簿；批量响应保留已完成的新鲜结果，隔离慢速候选。页面按有效期撤销达标状态，规则实质变化清除旧审核勾选。

原生接口依据：[Kalshi 订单簿](https://docs.kalshi.com/getting_started/orderbook_responses)、[Kalshi 生命周期](https://docs.kalshi.com/getting_started/market_lifecycle)、[Kalshi 费用舍入](https://docs.kalshi.com/getting_started/fee_rounding)、[Polymarket V2](https://docs.polymarket.com/v2-migration)、[Polymarket 市场描述](https://docs.polymarket.com/api-reference/markets/get-market-by-slug)。

页面展示净 ROI、两边最优价及对应挂单量、配对流动性、测算数量、均价、费用、投入金额、结算日、数据时间及未达标原因，提供明确的平台直达按钮。长规则保留换行并可完整阅读。

验证覆盖保存与恢复风控、当前政策筛选、手续费及流动性边界、结算缺失/超期、数据失效、规则补全、链接与前端交互。使用本地测试和模拟原生接口，不发送订单。
