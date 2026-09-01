# Kalshi 规则链接缺失兼容设计

## 背景与根因

Oddpool 候选已经能够正常拉取，但原生市场元数据核验在处理部分 Kalshi 市场时产生 `KeyError: 'rules_url'`。对提示中的多个真实 ticker 进行只读复现后，Kalshi 市场接口均返回 HTTP 200，并包含 `rules_primary` 和 `rules_secondary`，但响应中不再包含 `rules_url`。

当前 `normalize_kalshi_market()` 把 `rules_url` 当作必填字段，因此一个仅缺少展示链接、但规则正文完整的市场会在进入配对审核前被错误拒绝。

## 目标与安全边界

目标是兼容 Kalshi 当前响应结构，使规则正文完整的 Oddpool 候选能够继续进入人工审核草稿，同时不降低规则核验强度。

- `rules_primary` 继续作为必填规则正文；缺失或为空仍拒绝候选。
- 不使用 Oddpool 价格、规则或链接替代 Kalshi 原生规则正文。
- 不改变自动开仓门禁、交易模式或审核状态。
- 只处理 `rules_url` 缺失，不顺带放宽其他市场字段。

## 设计

`normalize_kalshi_market()` 先校验并读取 ticker 与 `rules_primary`。规则链接按以下顺序解析：

1. 如果 Kalshi 响应包含非空 `rules_url`，保留该官方值；
2. 否则使用 ticker 构造 `https://kalshi.com/markets/{ticker}`。

该回退值只用于展示和审计导航，不参与规则哈希、市场匹配、价格计算或订单提交。`NativePairMetadataResolver` 仍然从 Kalshi 官方 API 获取规则正文和结算元数据，并继续对 Oddpool 提供的原生标识进行交叉核验。

## 错误处理

- 缺少 ticker、`rules_primary`、tick size 或最小订单量时继续失败关闭；
- `rules_url` 缺失不再产生候选错误；
- 其他 Kalshi、Polymarket 或标识不匹配错误继续逐候选隔离，不阻塞有效候选；
- 错误信息不得包含 API Key 或其他凭证。

## 测试与验收

采用 TDD 添加回归覆盖：

1. 有完整规则正文但无 `rules_url` 的 Kalshi payload，修复前复现 `KeyError`；
2. 修复后生成基于 ticker 的公开链接，并保留原始规则正文；
3. payload 提供非空 `rules_url` 时仍优先使用原值；
4. 缺少或空白 `rules_primary` 时继续拒绝；
5. 元数据解析、Oddpool discovery 和后端全量测试通过；
6. 重启本地只读后端后，真实候选不再因 `'rules_url'` 失败，安全门禁保持 `read_only` 且 `opening_enabled=false`。
