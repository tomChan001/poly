# Cross-Market Control Plane

Kalshi 与 Polymarket 的受控跨平台执行系统。Oddpool 只用于发现候选；规则、订单簿、费用、余额和成交状态均以平台原生数据为准。

## 安全默认值

- 默认 `TRADING_MODE=read_only`，缺少配置时不会发送真实订单。
- 只有人工审核为 `EXACT` 且规则版本未变化的映射能进入执行评估。
- API 和前端不持有交易密钥；真实订单只能由 execution worker 发送。

## 本地开发

```powershell
Copy-Item .env.example .env
docker compose up -d postgres
uv sync --all-extras
uv run uvicorn backend.app.main:app --reload
```

前端使用独立终端：

```powershell
cd frontend
npm install
npm run dev
```

运行验证：

```powershell
uv run pytest -q
uv run ruff check .
uv run mypy backend/app
cd frontend
npm run build
```
