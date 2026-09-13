# 用户反馈修复实施计划

> 使用 subagent-driven-development 和 dispatching-parallel-agents 分派文件边界清晰的任务，主代理负责共享校验与集成。

**Goal:** 修复反馈中的流动性、筛选、规则、指标与平台跳转五项问题。

**Architecture:** 原生元数据与原生订单簿分别提供规则证据和经济测算；只读预审核返回当前政策下的测算，实际执行仍须原有人审和执行许可。

**Tech Stack:** Python FastAPI / dataclasses / PostgreSQL / React TypeScript。

- [x] 原生元数据：先补充规则、事件链接、最晚结算日期测试，修复 adapters/pair_metadata.py、adapters/kalshi/markets.py、services/executable_pairs.py 和 JSON 快照兼容性。
- [x] 风控：先写流动性保存恢复与拒绝边界测试；在 services/settings.py、api/routes/settings.py、db/risk_policy.py、db/tables.py 和新迁移增加 minimum_liquidity_contracts。
- [x] 预审核：新建 services/pair_previews.py，基于当前政策、原生簿与现有优化器生成只读测算；api/routes/pairs.py 和 container.py 连接该服务。执行复用相同日期、流动性校验。
- [x] 原生订单簿：测试 Kalshi orderbook_fp 美元字符串格式并修复解析，保留旧格式兼容。
- [x] 前端：用回归测试覆盖默认合格筛选、全量诊断、指标与链接；修改 PairSettingsPage、RiskSettingsPage、相关类型和样式，保留已有审核草稿行为。
- [x] 验证：运行后端 pytest、ruff、mypy，前端 vitest、lint、build 和页面交互检查；独立审核后修复发现的问题。

验证结果：后端 561 通过、4 个平台条件跳过；前端 91 通过；Rust 110 通过；浏览器 8 通过；打包约束及静态数据库迁移测试 52 通过、17 个子测试通过。后端 Ruff/Mypy、前端 lint/build 通过。

未运行真实 PostgreSQL 实例的集成测试，数据库迁移已提供且做了静态及保存恢复验证。Windows 环境验证了原生链接命令构造、权限和可移植编译；未生成或发布 macOS DMG，未实际启动 macOS 系统浏览器。没有调用真实下单。
