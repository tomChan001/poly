# Polymarket 原生元数据兼容设计

## 背景与根因

Kalshi 当前元数据兼容完成后，四个真实 Oddpool 候选不再出现
`rules_url`、`tick_size` 或 `minimum_order_size` 错误，但解析继续暴露两类
Polymarket 兼容问题。

第一类错误显示为 `Polymarket metadata is missing orderPriceMinTickSize`。只读检查
实际 Gamma 响应后确认字段并未缺失：`orderPriceMinTickSize` 的值是 JSON 小数，
`httpx.Response.json()` 将其解码为二进制 `float`，而 `_decimal_field()` 为避免精度
污染只接受字符串和整数。因此当前错误是数值类型误判，不是字段缺失。

第二类错误出现在 Floyd Mayweather 与 Manny Pacquiao 的市场。该市场原生 outcomes
是 `Mayweather` 和 `Pacquiao`，不是 `Yes` 和 `No`。Oddpool 已提供所选 Polymarket
token ID，而且该 ID 在 Gamma 的 `clobTokenIds` 中唯一存在；现有解析器却忽略这个
可交叉验证的标识，强制按逻辑 side `yes` 查找原生 outcome 标签，因此错误拒绝候选。

## 目标与安全边界

目标是兼容 Polymarket 当前 Gamma 响应的精确小数和命名 outcomes，使原生标识完整、
元数据合法的候选能够进入人工审核草稿。

- Polymarket condition ID 继续必须与 Oddpool 提供的 condition ID 唯一匹配。
- Oddpool token ID 只能作为待核验的原生标识；必须在 Gamma 原生 `clobTokenIds` 中
  唯一存在后才能采用。
- 不使用 Oddpool 价格、规则、最小数量或最小价格步长替代 Polymarket 原生数据。
- 不改变逻辑配对方向：`polymarket_outcome` 仍保留 Oddpool 的 `yes`/`no` side，实际
  下单与订单簿读取继续使用已经核验的原生 token ID。
- 不改变交易模式、自动开仓门禁、审核状态或订单提交路径。

## 选定方案

### Gamma JSON 精确解码

解析器不再直接对 Gamma markets/events 响应调用 `response.json()`。新增内部解码
边界，使用 `json.loads(response.text, parse_float=Decimal)`，使 JSON 小数从进入系统
开始就是精确十进制。非法的 `NaN`、`Infinity` 等 JSON 常量必须在解码阶段拒绝。

`_decimal_field()` 接受字符串、整数和已精确解码的 `Decimal`，继续拒绝布尔值和
二进制 `float`。字段存在但类型非法时应报告非法值，不能伪装成字段缺失：

- `orderMinSize` / `minimum_order_size` 必须是有限且大于零的十进制数；
- `orderPriceMinTickSize` / `minimum_tick_size` 必须是有限且满足 `0 < value <= 1`
  的十进制数；
- 两组候选字段都不存在时继续失败关闭。

本次不新增额外 CLOB 请求。真实 Gamma 数据已经提供所需字段；精确修正解码边界
能够解决当前问题，并避免每轮发现对每个候选增加网络调用。如果 Gamma 将来真正
移除这些字段，解析器仍应失败关闭，再针对官方原生端点单独设计兼容。

### Token 唯一交叉验证

`_polymarket_token()` 继续先解析 `outcomes` 与 `clobTokenIds`，要求两者都是字符串
数组且长度一致。

解析顺序调整为：

1. 如果 Oddpool leg 提供 `source_token_id`，要求它在 Gamma 原生 `clobTokenIds` 中
   恰好出现一次；唯一匹配后直接使用该原生 token ID。
2. 如果没有 `source_token_id`，保留原有行为：按逻辑 outcome 标签进行不区分大小写
   的唯一匹配。
3. token 不存在、重复、数组错位或无 token ID 且标签无法唯一匹配时继续拒绝候选。

这种方式允许 `Mayweather` / `Pacquiao` 等命名 outcomes，同时不盲信 Oddpool 标识。
现有 condition ID 核验和 token ID 最终一致性检查继续保留，形成双重防线。

## 数据流

1. 从 Oddpool 候选取得 Kalshi ticker、Polymarket event slug、condition ID 和可选 token ID。
2. 从 Kalshi 和 Gamma 拉取公开原生元数据。
3. 使用 condition ID 在 Gamma markets/events 中唯一选择市场。
4. 使用精确 JSON 小数解析原生最小数量和价格步长。
5. 有来源 token ID 时在原生 token 列表中唯一核验；否则使用 outcome 标签匹配。
6. 构造 `ExecutablePairInput`，保持 `quantity_step=1` 和 `PENDING_REVIEW` 工作流。

## 错误处理

- Gamma JSON 非法、包含非标准数值常量或不是对象数组时失败关闭。
- 数值字段缺失、非有限、非正、tick 大于 1、类型错误时失败关闭。
- condition ID 或 token ID 不能唯一匹配时失败关闭。
- 一个候选失败继续逐候选隔离，不阻塞其他候选。
- 错误消息不得包含 API key、私钥、passphrase 或其他凭据。

## 测试与验收

采用 TDD 添加以下回归覆盖：

1. Gamma JSON 中小数形式的 `orderPriceMinTickSize` 被精确解析为 `Decimal`；修复前
   复现当前 TypeError，修复后成功解析。
2. 字符串、整数和 `Decimal` 合法值继续工作；二进制 float、布尔值、非有限值、
   非正值以及 tick 大于 1 继续拒绝。
3. 命名 outcomes 与 Oddpool `source_token_id` 的唯一原生匹配成功；修复前复现
   `Polymarket outcome has no unique token: yes`。
4. 来源 token ID 缺失、重复或不存在时失败关闭；没有来源 token ID 时原有 Yes/No
   标签回退继续工作。
5. 四个真实候选完整解析成功，不再出现 Kalshi 三类旧错误或本次两类 Polymarket
   错误，并输出原生公开元数据用于核验。
6. 后端全量测试、Ruff、Mypy 和 `git diff --check` 通过。
7. 合并并重启只读实例后，`trading_mode=read_only`、`opening_enabled=false`，不产生
   订单提交。
