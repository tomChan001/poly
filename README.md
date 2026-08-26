# Cross-Market Control Plane

Kalshi 与 Polymarket 的受控跨平台执行系统。Oddpool 只用于发现候选；规则、订单簿、费用、余额和成交状态均以平台原生数据为准。

## 安全默认值

- 新安装默认 `TRADING_MODE=read_only` 且 `OPENING_ENABLED=false`；没有完整自动化证据时，启动门禁会把数据库开仓开关保持为关闭。
- 只有人工审核为 `EXACT` 且规则版本未变化的映射能进入执行评估。
- 前端只提供凭证的只写录入框，不保存凭证，也无法从 API 读回明文。
- PostgreSQL 只保存平台地址、账户标识和配置版本；Token、私钥和 passphrase 写入操作系统凭证库。
- API 仅在运营员提交配置和测试连接期间读取凭证；真实订单仍只能由 execution worker 发送。
- 除 `/health` 外的业务 API 只允许本机回环地址访问，远程请求返回 403。
- 人工只审核映射等价性与系统异常，不存在逐笔批准订单的 API 或页面。

## 本地开发

```powershell
Copy-Item .env.example .env
docker compose up -d postgres
uv sync --all-extras
uv run uvicorn backend.app.main:app --reload
```

首次启动和每次新增迁移后执行：

```powershell
uv run alembic upgrade head
```

前端使用独立终端：

```powershell
cd frontend
npm install
npm run dev
```

本机运营员可以在左侧“集成”菜单维护 Oddpool、Kalshi 和 Polymarket
配置。密码框始终为空；已保存凭证仅显示 SHA-256 指纹和配置状态。API、execution worker
以及其他需要读取凭证的进程必须使用同一个受控操作系统服务账户运行，否则它们无法访问同一凭证库。

### Polymarket Google / 邮箱账户

Google 或邮箱创建的 Polymarket 账户选择 `Google / Magic Proxy`。应用不会收集 Google 密码、OAuth token 或浏览器 cookie；请按 [Polymarket 官方说明](https://help.polymarket.com/en/articles/13364258-how-do-i-export-my-key) 导出 Magic signer 私钥。保存时后端会从私钥推导 owner，通过官方公开 profile 查询核对 proxy/funder，并固定使用 `signature_type=1`。

“测试连接（不会下单）”只派生/校验 L2 凭证并读取余额、allowance、订单和成交；它不会创建、取消订单或修改 allowance。没有完整账户映射、手续费规则、原生行情新鲜度或资金证据时，运行时会保留结构化拒绝记录并禁止执行。

本地 `.env.example` 开启 `LOCAL_SETUP_ENABLED=true`，用于从本机回环地址完成配置和运行控制。
远程请求始终拒绝，不提供远程登录入口。

运行验证：

```powershell
uv run pytest -q
uv run ruff check .
uv run mypy backend/app
cd frontend
npm run test
npm run lint
npm run build
```

上述自动化验证全部使用 fake trading ports 或只读接口，不发送真实订单。真实 canary 仍需单独授权和记录，不能由测试通过替代。
