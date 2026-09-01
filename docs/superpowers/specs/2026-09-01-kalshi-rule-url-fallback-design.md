# Kalshi 市场元数据兼容设计

## 背景与根因

Oddpool 候选已经能够正常拉取，但原生市场元数据核验在处理部分 Kalshi 市场时产生 `KeyError: 'rules_url'`。对提示中的多个真实 ticker 进行只读复现后，Kalshi 市场接口均返回 HTTP 200，并包含 `rules_primary` 和 `rules_secondary`，但响应中不再包含 `rules_url`。

当前 `normalize_kalshi_market()` 把 `rules_url`、`tick_size` 和 `minimum_order_size` 都当作必填字段，因此一个规则正文完整、但采用 Kalshi 当前固定点响应结构的市场会在进入配对审核前被错误拒绝。

第一轮 `rules_url` 修复完成后，使用四个真实 Oddpool 候选复测，`rules_url` 错误已归零，但同一调用链继续在缺失的 `tick_size` 处失败。Kalshi 当前响应以 `price_level_structure` 和 `price_ranges[].step` 表达有效价格增量，并且不再提供旧的 `minimum_order_size`。项目的共享执行策略仍只使用整份合约。

## 目标与安全边界

目标是兼容 Kalshi 当前响应结构，使规则正文完整的 Oddpool 候选能够继续进入人工审核草稿，同时不降低规则核验强度。

- `rules_primary` 继续作为必填规则正文；缺失或为空仍拒绝候选。
- 不使用 Oddpool 价格、规则或链接替代 Kalshi 原生规则正文。
- 不改变自动开仓门禁、交易模式或审核状态。
- 同时兼容 Kalshi 旧响应字段和当前固定点响应字段，不放宽规则正文及其他市场字段。

## 设计

`normalize_kalshi_market()` 先校验并读取 ticker 与 `rules_primary`。规则链接按以下顺序解析：

1. 如果 Kalshi 响应包含非空 `rules_url`，保留该官方值；
2. 否则使用 ticker 构造 `https://kalshi.com/markets/{ticker}`。

该回退值只用于展示和审计导航，不参与规则哈希、市场匹配、价格计算或订单提交。`NativePairMetadataResolver` 仍然从 Kalshi 官方 API 获取规则正文和结算元数据，并继续对 Oddpool 提供的原生标识进行交叉核验。

### 价格增量

价格增量按以下顺序解析：

1. 如果旧响应包含合法 `tick_size`，继续使用该值；
2. 否则要求 `price_ranges` 为非空对象数组，并校验每段 `start`、`end`、`step` 都是 0 到 1 之间的有限十进制数；
3. 每段必须满足 `start < end`、`step > 0`，并且 step 不得大于该段宽度；
4. 使用所有分段中最小的正数 step 作为 `MarketMetadata.minimum_tick`。

单一 `minimum_tick` 只作为原生元数据与材料指纹的一部分；本次改动不新增分段报价或改变订单价格生成逻辑。非法或缺失的两种 tick 表达都失败关闭。

### 最小数量

如果旧响应包含合法 `minimum_order_size`，继续使用。当前响应缺失该字段时使用 `1` 作为安全默认值。`NativePairMetadataResolver` 现有逻辑仍将跨平台最小数量与 `Decimal(1)` 取最大值，并保持 `quantity_step=1`，因此本次兼容不会启用 Kalshi 分数合约。

## 错误处理

- 缺少 ticker、`rules_primary` 或合法 tick 表达时继续失败关闭；
- `rules_url` 缺失不再产生候选错误；
- 仅 `minimum_order_size` 缺失时使用安全默认值 1；字段存在但非法时继续拒绝；
- 其他 Kalshi、Polymarket 或标识不匹配错误继续逐候选隔离，不阻塞有效候选；
- 错误信息不得包含 API Key 或其他凭证。

## 测试与验收

采用 TDD 添加回归覆盖：

1. 有完整规则正文但无 `rules_url` 的 Kalshi payload，修复前复现 `KeyError`；
2. 修复后生成基于 ticker 的公开链接，并保留原始规则正文；
3. payload 提供非空 `rules_url` 时仍优先使用原值；
4. 缺少或空白 `rules_primary` 时继续拒绝；
5. 无旧 `tick_size`、但有合法单段或多段 `price_ranges` 时解析最小 step；
6. 空数组、非法十进制、非正 step、反向范围或越界范围继续拒绝；
7. 缺少 `minimum_order_size` 时得到安全值 1，旧合法值仍优先；
8. 元数据解析、Oddpool discovery 和后端全量测试通过；
9. 重启本地只读后端后，真实候选不再因 `'rules_url'`、`'tick_size'` 或 `'minimum_order_size'` 失败，安全门禁保持 `read_only` 且 `opening_enabled=false`。
