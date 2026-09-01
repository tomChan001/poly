# Kalshi x Polymarket 受控跨平台执行系统实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建成一个以结算规则严格等价为前提，能从 Oddpool 发现候选、用 Kalshi/Polymarket 原生数据复核、计算保守净 ROI，并按阶段从只读观测升级到受限自动执行的系统。

**Architecture:** 采用模块化单体仓库，API、行情采集、机会计算、执行器作为独立进程部署，共享 PostgreSQL 中的持久状态和审计日志。Oddpool（审阅报告中写作 Oddspool）只进入发现层；规则、订单簿、费用、余额和成交状态全部以平台原生接口为准。真实交易默认在代码和配置两层关闭，只有 `EXACT` 映射及全部硬风控通过后才可能进入执行状态机。

**Tech Stack:** Python 3.12、FastAPI、Pydantic 2、SQLAlchemy 2、Alembic、PostgreSQL 16、httpx/websockets、React + TypeScript + Vite、TanStack Query/Table、pytest、Playwright、Docker Compose、Prometheus/OpenTelemetry。

---

## 1. 结论与范围

原需求中 `Price Yes + Price No < 1` 只能作为发现信号，不能直接触发交易。系统必须先证明两个合约在所有可能状态下严格互补，并在下单时使用两端实时可执行深度、实际费率、余额和保守缓冲重算。

第一版按以下边界开发：

- 纳入：Oddpool 候选发现、原生市场元数据、规则版本、人工映射审核、L2 订单簿、费用计算、余额与预留、保守报价、影子执行、审核通过后的受限自动执行、告警、对账、审计和紧急停机。
- 暂不纳入：自动用大模型批准映射、跨平台转账、自动充值、做市、三平台以上组合、移动端 App、机器学习预测价格、未经人工验证的全自动交易。
- 任一映射不是 `EXACT`、规则版本变化、行情过期、余额不足、账户对账异常或系统已存在未对冲头寸时，禁止新开仓。
- 金额、价格、数量和费率全部使用 `Decimal` 或数据库 `NUMERIC`；不得使用二进制浮点数。

## 2. 上线前必须由业务方提供的输入

这些输入不会阻塞只读 MVP，但会阻塞对应的联调或真实交易：

| 输入 | 最迟提供阶段 | 未提供时的处理 |
|---|---|---|
| Oddpool API 文档、测试凭证、限流和条款 | Task 4 | 使用固定 JSON fixture 完成发现层，不接真实 Oddpool |
| Kalshi/Polymarket 账户适用资格与地域/条款确认记录 | Task 11 | `TRADING_MODE` 只能保持 `read_only` |
| 两端只读凭证 | Task 9 | 使用公开行情，跳过余额与用户流联调 |
| 两端最小权限交易凭证/钱包签名方案 | Task 11 | 禁止构建真实订单 |
| 单笔资金上限、单事件上限、总未结算资本上限、最大未对冲损失 | Task 11 | 使用测试默认值 `$10/$25/$100/$2`，且仅允许测试环境 |
| 最低保守净 ROI、最长期限、行情最大年龄 | Task 8 | 初始测试值 `3%/30天/2秒`，生产必须由操作者显式确认 |
| 通知渠道 Webhook URL | Task 8 | 写入审计日志和本地控制台，不影响只读流程 |

## 3. 目标仓库结构

```text
poly/
  README.md
  .env.example
  compose.yaml
  pyproject.toml
  alembic.ini
  backend/
    app/
      api/                 # FastAPI 路由、鉴权、请求/响应 DTO
      core/                # 配置、Decimal、时钟、日志、安全开关
      domain/              # 领域枚举、实体、状态转换、风控规则
      db/                  # SQLAlchemy 模型、仓储、事务、outbox
      adapters/
        oddpool/           # 只负责发现候选
        kalshi/            # 元数据、规则、L2、账户、订单
        polymarket/        # 元数据、规则、L2、账户、订单
        notifications/     # Webhook/控制台通知
      services/            # 映射、报价、影子执行、对账、真实执行
      workers/             # discovery、market_data、engine、execution
    tests/
      unit/
      integration/
      fixtures/
  frontend/
    src/
      api/
      components/
      pages/
      routes/
      types/
    tests/
  migrations/
  observability/
  docs/
    architecture/
    runbooks/
    superpowers/plans/
```

职责约束：平台差异只能存在于 `adapters/<venue>`；业务服务只能消费标准化模型。真实下单只能由 `workers/execution` 调用，Web API 不能直接持有交易私钥或调用下单接口。

## 4. 核心流程

```text
Oddpool 候选
      |
      v
原生 ID/规则解析 --> PENDING_REVIEW --> 人工审核 --> EXACT
                                                |
                                                v
两端实时 L2 --> 同步快照校验 --> 费用/深度优化 --> 可执行报价
                                                |
                         +----------------------+----------------------+
                         |                                             |
                         v                                             v
                    影子执行/统计                                  受限自动执行
                                                                       |
                                                                       v
 DISCOVERED -> ELIGIBLE -> PRECHECKED -> SUBMITTED -> PAIRED
                                            |              |
                                            +-> PARTIALLY_HEDGED -> EXCEPTION
```

## 5. 统一数据口径

保守报价对数量 `N` 使用：

```text
C_K(N) = Kalshi 逐档成交成本 + Kalshi 实际费率
C_P(N) = Polymarket 逐档成交成本 + Polymarket 实际费率
profit_floor(N) = N - C_K(N) - C_P(N) - explicit_cost(N) - risk_buffer(N)
conservative_roi(N) = profit_floor(N) / [C_K(N) + C_P(N) + explicit_cost(N)]
matched_quantity = min(filled_kalshi, filled_polymarket)
```

数量优化规则：枚举两端订单簿累计深度的联合断点，只保留满足最小数量、步长、资金、风险限额和最低 ROI 的数量；在合格数量中选 `profit_floor` 最大者。候选执行队列按 `conservative_roi` 降序，ROI 相同则按 `profit_floor` 降序。界面同时展示最优数量、两端 VWAP、两端费用、风险缓冲、净利润下界、ROI、资本占用天数和拒绝原因。

## 6. 开发任务

### Task 1: 初始化仓库与可重复开发环境

**Files:**
- Create: `README.md`
- Create: `.env.example`
- Create: `pyproject.toml`
- Create: `compose.yaml`
- Create: `backend/app/main.py`
- Create: `backend/app/core/config.py`
- Create: `backend/tests/unit/test_health.py`
- Create: `frontend/package.json`
- Create: `frontend/src/main.tsx`

- [ ] **Step 1: 初始化 Git、Python 和前端工程**

Run:

```powershell
git init
uv init --python 3.12
uv add fastapi uvicorn pydantic-settings sqlalchemy alembic asyncpg httpx websockets structlog tenacity prometheus-client
uv add --dev pytest pytest-asyncio pytest-cov ruff mypy respx
npm create vite@latest frontend -- --template react-ts
```

Expected: 根目录出现 Git 仓库、`uv.lock` 和可启动的 Vite 工程。

- [ ] **Step 2: 先写健康检查测试**

```python
from fastapi.testclient import TestClient
from backend.app.main import app


def test_health() -> None:
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "trading_mode": "read_only"}
```

- [ ] **Step 3: 运行测试确认失败**

Run: `uv run pytest backend/tests/unit/test_health.py -q`

Expected: FAIL，原因是应用或 `/health` 尚未定义。

- [ ] **Step 4: 实现配置和健康检查**

```python
# backend/app/core/config.py
from enum import StrEnum
from pydantic_settings import BaseSettings, SettingsConfigDict


class TradingMode(StrEnum):
    READ_ONLY = "read_only"
    SHADOW = "shadow"
    LIMITED_AUTO = "limited_auto"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    trading_mode: TradingMode = TradingMode.READ_ONLY
    database_url: str = "postgresql+asyncpg://poly:poly@localhost:5432/poly"


settings = Settings()
```

```python
# backend/app/main.py
from fastapi import FastAPI
from backend.app.core.config import settings

app = FastAPI(title="Cross-Market Control Plane")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "trading_mode": settings.trading_mode.value}
```

```yaml
# compose.yaml
services:
  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: poly
      POSTGRES_USER: poly
      POSTGRES_PASSWORD: poly
    ports:
      - "5432:5432"
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U poly -d poly"]
      interval: 2s
      timeout: 2s
      retries: 15
    volumes:
      - poly-postgres:/var/lib/postgresql/data

volumes:
  poly-postgres:
```

`.env.example` 固定包含 `TRADING_MODE=read_only` 和无生产凭证的本地 `DATABASE_URL`；README 明确真实交易默认关闭。

- [ ] **Step 5: 验证并提交**

Run: `uv run pytest backend/tests/unit/test_health.py -q`

Expected: `1 passed`。

```powershell
git add README.md .env.example pyproject.toml uv.lock compose.yaml backend frontend
git commit -m "chore: bootstrap controlled execution platform"
```

### Task 2: 建立领域类型、数据库模型与不可变审计日志

**Files:**
- Create: `backend/app/domain/enums.py`
- Create: `backend/app/domain/models.py`
- Create: `backend/app/db/base.py`
- Create: `backend/app/db/tables.py`
- Create: `backend/app/db/audit.py`
- Create: `migrations/versions/0001_initial_schema.py`
- Test: `backend/tests/unit/domain/test_execution_state.py`
- Test: `backend/tests/integration/db/test_initial_schema.py`

- [ ] **Step 1: 写状态转换失败测试**

```python
import pytest
from backend.app.domain.enums import ExecutionState
from backend.app.domain.models import validate_transition


def test_partially_hedged_cannot_return_to_prechecked() -> None:
    with pytest.raises(ValueError, match="invalid transition"):
        validate_transition(ExecutionState.PARTIALLY_HEDGED, ExecutionState.PRECHECKED)


def test_prechecked_can_be_submitted() -> None:
    validate_transition(ExecutionState.PRECHECKED, ExecutionState.SUBMITTED)
```

- [ ] **Step 2: 定义固定枚举和唯一状态图**

```python
from enum import StrEnum


class Venue(StrEnum):
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"


class MappingStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    EXACT = "exact"
    CONDITIONAL = "conditional"
    REJECTED = "rejected"
    STALE = "stale"


class ExecutionState(StrEnum):
    DISCOVERED = "discovered"
    ELIGIBLE = "eligible"
    PRECHECKED = "prechecked"
    SUBMITTED = "submitted"
    PAIRED = "paired"
    PARTIALLY_HEDGED = "partially_hedged"
    EXCEPTION = "exception"
    CANCELLED = "cancelled"
```

```python
ALLOWED_TRANSITIONS = {
    ExecutionState.DISCOVERED: {ExecutionState.ELIGIBLE, ExecutionState.CANCELLED},
    ExecutionState.ELIGIBLE: {ExecutionState.PRECHECKED, ExecutionState.CANCELLED},
    ExecutionState.PRECHECKED: {ExecutionState.SUBMITTED, ExecutionState.CANCELLED},
    ExecutionState.SUBMITTED: {
        ExecutionState.PAIRED,
        ExecutionState.PARTIALLY_HEDGED,
        ExecutionState.EXCEPTION,
    },
    ExecutionState.PARTIALLY_HEDGED: {ExecutionState.PAIRED, ExecutionState.EXCEPTION},
    ExecutionState.PAIRED: set(),
    ExecutionState.EXCEPTION: set(),
    ExecutionState.CANCELLED: set(),
}


def validate_transition(current: ExecutionState, target: ExecutionState) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        raise ValueError(f"invalid transition: {current} -> {target}")
```

- [ ] **Step 3: 建立初始表结构**

迁移必须创建以下表及外键：`venue_market`、`rule_version`、`pair_mapping`、`mapping_review`、`discovery_candidate`、`book_snapshot`、`quote_evaluation`、`balance_snapshot`、`capital_reservation`、`execution`、`execution_leg`、`fill`、`state_transition`、`audit_event`、`outbox_event`、`system_control`。价格/数量列用 `NUMERIC(38,18)`；原始 API 数据用 `JSONB`；所有业务表含 UTC `created_at`，版本表含内容 SHA-256。

- [ ] **Step 4: 保证审计事件只能追加**

数据库迁移为 `audit_event` 添加阻止 `UPDATE/DELETE` 的 trigger；应用接口只暴露 `append_audit_event()`，入参包含 `correlation_id`、actor、事件类型、对象类型/ID、前后状态、原始请求/响应哈希。

- [ ] **Step 5: 验证迁移与提交**

Run:

```powershell
docker compose up -d postgres
uv run alembic upgrade head
uv run pytest backend/tests/unit/domain/test_execution_state.py backend/tests/integration/db/test_initial_schema.py -q
```

Expected: 所有测试通过，数据库包含 16 张业务表，审计表更新测试被数据库拒绝。

```powershell
git add backend/app/domain backend/app/db migrations backend/tests
git commit -m "feat: add durable domain and audit model"
```

### Task 3: 定义平台标准接口与 Decimal 正规化

**Files:**
- Create: `backend/app/domain/ports.py`
- Create: `backend/app/domain/market.py`
- Create: `backend/app/core/decimal.py`
- Test: `backend/tests/unit/domain/test_orderbook_normalization.py`

- [ ] **Step 1: 写 Kalshi 隐含卖价测试**

```python
from decimal import Decimal
from backend.app.domain.market import BookLevel, kalshi_asks_from_opposite_bids


def test_kalshi_no_ask_is_one_minus_yes_bid() -> None:
    result = kalshi_asks_from_opposite_bids([BookLevel(Decimal("0.24"), Decimal("505"))])
    assert result == [BookLevel(Decimal("0.76"), Decimal("505"))]
```

- [ ] **Step 2: 定义标准模型与接口**

```python
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import AsyncIterator, Protocol


@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True)
class NormalizedBook:
    market_id: str
    outcome: str
    sequence: str
    captured_at: datetime
    asks: tuple[BookLevel, ...]


class MarketDataPort(Protocol):
    async def get_market(self, market_id: str): ...
    async def get_book(self, market_id: str, outcome: str) -> NormalizedBook: ...
    def stream_books(self, market_ids: set[str]) -> AsyncIterator[NormalizedBook]: ...


class TradingPort(Protocol):
    async def get_available_balance(self): ...
    async def submit_fok(self, request): ...
    async def cancel(self, venue_order_id: str): ...
    async def list_open_orders(self): ...
    async def list_recent_fills(self, since: datetime): ...
```

- [ ] **Step 3: 实现严格的 Decimal 解析**

`parse_decimal()` 仅接受字符串或整数，拒绝 `float`、NaN、Infinity 和负数量；价格必须位于 `[0, 1]`，订单簿按价格升序且同价位合并。

- [ ] **Step 4: 运行单元测试和类型检查**

Run: `uv run pytest backend/tests/unit/domain/test_orderbook_normalization.py -q; uv run mypy backend/app/domain backend/app/core`

Expected: 测试通过，mypy 无错误。

- [ ] **Step 5: 提交**

```powershell
git add backend/app/domain backend/app/core backend/tests/unit/domain
git commit -m "feat: define normalized venue ports"
```

### Task 4: 接入 Oddpool 候选发现层

**Files:**
- Create: `backend/app/adapters/oddpool/client.py`
- Create: `backend/app/adapters/oddpool/schema.py`
- Create: `backend/app/services/discovery.py`
- Create: `backend/app/workers/discovery.py`
- Create: `backend/tests/fixtures/oddpool/opportunities.json`
- Test: `backend/tests/unit/adapters/test_oddpool.py`
- Test: `backend/tests/integration/services/test_discovery.py`

- [ ] **Step 1: 用脱敏 API 响应建立 contract fixture**

Fixture 必须保留候选 ID、事件标题、outcome、两端方向、平台链接、扫描价格、扫描费用、扫描时间和预计结算时间。若真实 API 字段不同，只修改 `schema.py`，不得让 Oddpool DTO 泄漏到领域层。

- [ ] **Step 2: 写幂等导入测试**

同一 `source_candidate_id + source_updated_at` 导入两次只能产生一条 `discovery_candidate`；扫描价格只作为展示证据，不能写入 `quote_evaluation`。

- [ ] **Step 3: 实现客户端和 60 秒轮询**

轮询器使用 0-10 秒随机抖动；记录响应时间、数据源时间、HTTP 状态与限流头；429 时按 `1s, 2s, 4s, 8s, 16s, 60s` 退避。链接解析失败时保存候选并标记 `source_link_invalid`，不得丢弃证据或猜测映射。

- [ ] **Step 4: 加入数据新鲜度指标**

导出 `oddpool_poll_total`、`oddpool_poll_errors_total`、`oddpool_candidate_age_seconds` 和 `oddpool_candidates_imported_total`。

- [ ] **Step 5: 验证与提交**

Run: `uv run pytest backend/tests/unit/adapters/test_oddpool.py backend/tests/integration/services/test_discovery.py -q`

Expected: fixture 可导入、重复请求幂等、429 会退避、坏链接进入审核队列。

```powershell
git add backend/app/adapters/oddpool backend/app/services/discovery.py backend/app/workers/discovery.py backend/tests
git commit -m "feat: ingest discovery candidates from oddpool"
```

### Task 5: 接入原生市场元数据、规则快照与映射审核

**Files:**
- Create: `backend/app/adapters/kalshi/markets.py`
- Create: `backend/app/adapters/polymarket/markets.py`
- Create: `backend/app/services/rules.py`
- Create: `backend/app/services/mappings.py`
- Create: `backend/app/api/routes/mappings.py`
- Create: `frontend/src/pages/MappingQueuePage.tsx`
- Create: `frontend/src/pages/MappingReviewPage.tsx`
- Test: `backend/tests/unit/services/test_rule_versioning.py`
- Test: `backend/tests/integration/api/test_mapping_review.py`
- Test: `frontend/tests/mapping-review.spec.ts`

- [ ] **Step 1: 写规则变化自动失效测试**

```python
async def test_changed_rule_hash_invalidates_exact_mapping(rule_service, exact_mapping):
    await rule_service.record_version(
        market_id=exact_mapping.kalshi_market_id,
        text="changed official rule text",
        source_url="https://official.example/rule",
    )
    refreshed = await rule_service.get_mapping(exact_mapping.id)
    assert refreshed.status == "stale"
```

- [ ] **Step 2: 采集并版本化官方规则**

每个平台市场保存稳定 ID、标题、outcomes、状态、最小 tick/数量、最早到期、预计结算、最坏结算、规则 URL、完整规则文本、数据源、时区、取消/无结果条款和 SHA-256。规则正文或关键元数据哈希变化时，所有引用该版本的 `EXACT` 映射原子更新为 `STALE`。

- [ ] **Step 3: 实现三档审核 API**

审核请求必须提交 `status`、Kalshi 方向、Polymarket 方向、审核清单、边界状态真值表、备注和当前两个规则版本 ID。API 只允许人工把映射设为 `EXACT`；任何自动化进程最多可建议 `CONDITIONAL`。

审核清单固定包含：事件主体、阈值及边界、时间窗/时区、正式发生与宣布的区别、数据源、延期、取消、无结果、争议流程、兑付单位。所有项均明确匹配且真值表所有行合计兑付为 `$1` 才允许 `EXACT`。

- [ ] **Step 4: 实现审核页面**

页面左右并排显示两端完整规则、差异高亮、稳定 ID、原始链接与规则版本；下方显示清单和真值表。主操作为“批准 EXACT”“标记 CONDITIONAL”“拒绝”，规则过期时隐藏批准按钮并要求刷新。

- [ ] **Step 5: 验证与提交**

Run:

```powershell
uv run pytest backend/tests/unit/services/test_rule_versioning.py backend/tests/integration/api/test_mapping_review.py -q
cd frontend
npx playwright test tests/mapping-review.spec.ts
```

Expected: 规则变化使旧批准失效；未完成清单无法批准；审核页面在桌面和移动视口无溢出。

```powershell
git add backend/app/adapters backend/app/services backend/app/api frontend/src frontend/tests backend/tests
git commit -m "feat: add versioned settlement equivalence review"
```

### Task 6: 接入两端 L2 行情流并保证快照一致性

**Files:**
- Create: `backend/app/adapters/kalshi/orderbook.py`
- Create: `backend/app/adapters/polymarket/orderbook.py`
- Create: `backend/app/services/orderbooks.py`
- Create: `backend/app/workers/market_data.py`
- Test: `backend/tests/fixtures/kalshi/orderbook.json`
- Test: `backend/tests/fixtures/polymarket/orderbook.json`
- Test: `backend/tests/unit/adapters/test_kalshi_orderbook.py`
- Test: `backend/tests/unit/adapters/test_polymarket_orderbook.py`
- Test: `backend/tests/integration/services/test_book_resync.py`

- [ ] **Step 1: 写订单簿正规化和断序测试**

覆盖 Kalshi 对侧 bid 推导 ask、Polymarket ask 排序、同价合并、零数量删除、增量序列缺口、断线重连和快照年龄计算。

- [ ] **Step 2: 实现 REST 快照 + WebSocket 增量**

每个市场先拉 REST 快照，再应用序列连续的增量；发现序列缺口或断线即把 book 标记为 `UNSAFE`，清空未确认增量并重新拉快照。重建完成前机会引擎不得使用该 book。

- [ ] **Step 3: 实现下单前同步校验**

`get_synchronized_books()` 必须检查两端市场可交易、规则版本未变、book 状态安全、两端本地接收时间差不超过 500ms、各自年龄不超过配置值；否则返回结构化拒绝码而不是空值。

- [ ] **Step 4: 保留可复算快照**

每次产生可交易信号或拒绝决定时，保存两端原始响应哈希、标准化档位、sequence、平台时间和本地单调时钟差。普通行情增量只保留 7 天；参与决策的证据保留至少 7 年或按合规要求更长。

- [ ] **Step 5: 验证与提交**

Run: `uv run pytest backend/tests/unit/adapters/test_kalshi_orderbook.py backend/tests/unit/adapters/test_polymarket_orderbook.py backend/tests/integration/services/test_book_resync.py -q`

Expected: 断序期间不产生报价，重同步后才恢复；所有金额保持 Decimal 精度。

```powershell
git add backend/app/adapters backend/app/services/orderbooks.py backend/app/workers/market_data.py backend/tests
git commit -m "feat: add synchronized native order books"
```

### Task 7: 实现费用引擎、深度吃单和最优数量计算

**Files:**
- Create: `backend/app/domain/quote.py`
- Create: `backend/app/services/fees.py`
- Create: `backend/app/services/optimizer.py`
- Create: `backend/tests/unit/services/test_fees.py`
- Create: `backend/tests/unit/services/test_optimizer.py`
- Create: `backend/tests/golden/quote_cases.json`

- [ ] **Step 1: 先写需求样例 golden test**

```python
from decimal import Decimal


def test_document_example(calculator):
    result = calculator.from_filled_costs(
        quantity=Decimal("268"),
        kalshi_cost=Decimal("213.36"),
        kalshi_fee=Decimal("3.05"),
        polymarket_cost=Decimal("37.52"),
        polymarket_fee=Decimal("0"),
        explicit_cost=Decimal("0"),
        risk_buffer=Decimal("0"),
    )
    assert result.profit_floor == Decimal("14.07")
    assert result.roi.quantize(Decimal("0.0001")) == Decimal("0.0554")
```

- [ ] **Step 2: 建立版本化费用策略**

费用引擎输入必须包含 venue、market/series、side、maker/taker、价格、数量和生效时间；输出费用与规则版本 ID。平台返回的实际费用在成交后写入 fill，并与估算值比较。费率不匹配超过 `$0.01` 时暂停新交易并告警。不得写死“Polymarket 费用为 0”。

- [ ] **Step 3: 实现逐档成本函数**

```python
def sweep_cost(levels, quantity):
    remaining = quantity
    notional = Decimal("0")
    for level in levels:
        take = min(remaining, level.quantity)
        notional += take * level.price
        remaining -= take
        if remaining == 0:
            break
    if remaining > 0:
        raise InsufficientDepth(remaining)
    return notional
```

- [ ] **Step 4: 实现优化约束与拒绝码**

候选数量来自两端累计档位联合断点并向数量步长取整。依次检查 `MAPPING_NOT_EXACT`、`RULE_VERSION_CHANGED`、`MARKET_NOT_OPEN`、`STALE_BOOK`、`INSUFFICIENT_DEPTH`、`FEE_UNKNOWN`、`ROI_BELOW_THRESHOLD`、`BALANCE_SHORTAGE`、`EVENT_LIMIT`、`PORTFOLIO_LIMIT`、`UNHEDGED_POSITION_EXISTS`。结果必须包含所有失败原因，界面不得只显示“不可交易”。

- [ ] **Step 5: 验证性质和提交**

Run: `uv run pytest backend/tests/unit/services/test_fees.py backend/tests/unit/services/test_optimizer.py -q`

Expected: 需求样例为 `$14.07/5.54%`；增加数量时成本不会下降；深度不足不会返回部分报价；费用按平台规则向有利于安全的一侧取整。

```powershell
git add backend/app/domain/quote.py backend/app/services backend/tests
git commit -m "feat: calculate executable conservative quotes"
```

### Task 8: 构建只读机会面板、策略配置和通知

**Files:**
- Create: `backend/app/api/routes/opportunities.py`
- Create: `backend/app/api/routes/settings.py`
- Create: `backend/app/services/notifications.py`
- Create: `frontend/src/pages/OpportunitiesPage.tsx`
- Create: `frontend/src/pages/OpportunityDetailPage.tsx`
- Create: `frontend/src/pages/RiskSettingsPage.tsx`
- Create: `frontend/src/components/TradingModeBanner.tsx`
- Test: `backend/tests/integration/api/test_opportunities.py`
- Test: `frontend/tests/opportunities.spec.ts`

- [ ] **Step 1: 定义只读 API**

列表字段固定为事件、两端方向、映射状态、最优数量、两端 VWAP/费用、总投入、兑付、净利润下界、保守 ROI、最早/预计/最坏结算、资本占用日收益、行情年龄、状态和拒绝原因。详情返回两端订单簿与每个数量断点的计算过程。

- [ ] **Step 2: 实现策略配置版本**

配置包括最低 ROI、最长预计/最坏结算天数、单笔/单事件/总资本上限、行情最大年龄、显式成本、风险缓冲、最大未对冲秒数和最大未对冲损失。每次修改生成不可变版本；每个 `quote_evaluation` 引用具体版本，禁止覆盖历史配置。

- [ ] **Step 3: 实现工作台 UI**

首屏直接显示工作台，不做营销页。用表格支持按状态、ROI、结算期、映射状态筛选；详情用全宽分区展示规则证据、L2 深度、计算分解和审计时间线。顶部固定显示 `READ ONLY/SHADOW/LIMITED AUTO` 和 kill switch 状态。

- [ ] **Step 4: 实现 outbox 通知**

通知触发包括：新候选待审核、`EXACT` 机会达标、规则变化、余额不足/需充值、行情断流、部分对冲、对账异常、kill switch 变化。通知 payload 不包含 API 密钥、签名材料或完整账户凭证；失败按 1/2/4/8/16 分钟重试并可人工重放。

- [ ] **Step 5: 验证与提交**

Run:

```powershell
uv run pytest backend/tests/integration/api/test_opportunities.py -q
cd frontend
npx playwright test tests/opportunities.spec.ts --project=chromium
```

Expected: 1440x900 和 390x844 均无文本重叠；排序默认 ROI 降序；每个拒绝结果可解释；通知幂等。

```powershell
git add backend/app/api backend/app/services/notifications.py frontend/src frontend/tests backend/tests
git commit -m "feat: add read-only opportunity control plane"
```

### Task 9: 实现影子执行、历史回放与盈利验证指标

**Files:**
- Create: `backend/app/services/shadow_execution.py`
- Create: `backend/app/services/replay.py`
- Create: `backend/app/workers/engine.py`
- Create: `backend/app/api/routes/analytics.py`
- Create: `frontend/src/pages/AnalyticsPage.tsx`
- Test: `backend/tests/unit/services/test_shadow_execution.py`
- Test: `backend/tests/integration/services/test_replay.py`

- [ ] **Step 1: 写不可偷看未来的回放测试**

给定时间序列时，决策只能读取 `decision_at` 之前收到的规则、book、费用和余额版本；测试在决策后一毫秒注入更优价格，确保结果不变。

- [ ] **Step 2: 实现影子执行器**

影子执行生成与真实执行相同的 `execution`、legs 和状态变更，但 adapter 为 `ShadowTradingPort`，永不访问真实下单端点。模拟以当时可见 ask 和 FOK 限价计算，明确标记其不能证明真实成交。

- [ ] **Step 3: 实现历史重放**

输入为候选版本、规则版本、两端 book 快照、费用版本、策略版本和余额快照；输出必须与在线 `quote_evaluation` 逐字段一致。任何差异保存为审计异常。

- [ ] **Step 4: 暴露四类核心指标**

仪表板显示机会存续时间、可执行净 ROI 分布、影子双腿可成交率、每美元资本占用日净收益。另显示 `EXACT` 审核率、过期行情拒绝数、费用差异和映射变化次数，禁止只展示理论年化收益。

- [ ] **Step 5: 连续运行和提交**

Run: `uv run pytest backend/tests/unit/services/test_shadow_execution.py backend/tests/integration/services/test_replay.py -q`

Expected: 在线与回放逐字段相同；shadow 模式没有任何真实订单 HTTP 请求。

```powershell
git add backend/app/services backend/app/workers/engine.py backend/app/api frontend/src backend/tests
git commit -m "feat: add shadow execution and deterministic replay"
```

### Task 10: 实现余额、资本预留和账户全量对账

**Files:**
- Create: `backend/app/adapters/kalshi/account.py`
- Create: `backend/app/adapters/polymarket/account.py`
- Create: `backend/app/services/capital.py`
- Create: `backend/app/services/reconciliation.py`
- Create: `backend/app/workers/reconciliation.py`
- Test: `backend/tests/unit/services/test_capital.py`
- Test: `backend/tests/integration/services/test_reconciliation.py`

- [ ] **Step 1: 写开放订单占用资金测试**

可用资本必须等于平台可用余额与本地账本中更保守的值，并扣除 `reserved_for_open_orders`、`reserved_for_hedge_buffer`、`reserved_for_fees` 和 `unsettled_capital`。同一个 correlation ID 重试不得重复预留。

- [ ] **Step 2: 实现事务性双边预留**

在单个数据库事务中锁定两端账户行，检查版本未变化，分别创建预留；任一侧失败则整体回滚。预留有短 TTL，但 `SUBMITTED` 后只能由成交/撤单/人工异常处置释放。

- [ ] **Step 3: 接入认证用户流和 REST 对账**

用户流用于低延迟订单/成交更新；每次重连后必须 REST 拉取余额、开放订单和近期成交全量对账。常态每 60 秒对账一次，`SUBMITTED` 或 `PARTIALLY_HEDGED` 时每秒对账。

- [ ] **Step 4: 实现全局暂停规则**

平台数据与本地账本在订单、成交、可用余额任一项不一致时，原子设置 `system_control.opening_enabled=false`，发送高优先级告警；差异完成排障并通过一次全量对账前不能恢复。

- [ ] **Step 5: 验证与提交**

Run: `uv run pytest backend/tests/unit/services/test_capital.py backend/tests/integration/services/test_reconciliation.py -q`

Expected: 并发机会不会超额预留；重连能补齐遗漏成交；任何差异会关闭开仓。

```powershell
git add backend/app/adapters backend/app/services backend/app/workers/reconciliation.py backend/tests
git commit -m "feat: reserve and reconcile cross-venue capital"
```

### Task 11: 构建审核后自动触发的双腿真实执行状态机

**Files:**
- Create: `backend/app/adapters/kalshi/trading.py`
- Create: `backend/app/adapters/polymarket/trading.py`
- Create: `backend/app/services/execution.py`
- Create: `backend/app/services/emergency_hedge.py`
- Create: `backend/app/workers/execution.py`
- Create: `backend/app/api/routes/executions.py`
- Create: `frontend/src/pages/ExecutionDetailPage.tsx`
- Create: `docs/runbooks/partially-hedged.md`
- Test: `backend/tests/unit/services/test_execution_state_machine.py`
- Test: `backend/tests/integration/services/test_execution_faults.py`

- [ ] **Step 1: 用 fake adapters 写故障矩阵测试**

至少覆盖：两腿全成、A 成/B 拒、A 拒/B 成、两腿均拒、单腿部分成交、响应超时但实际成交、重复成交回报、进程在提交后崩溃、用户流断线、费用超估和规则在确认后变化。断言 matched 数量只取两端最小成交量。

- [ ] **Step 2: 实现短时执行授权与自动触发**

机会引擎只对当前仍为 `EXACT` 的映射自动生成 2 秒、单次使用、绑定 `quote_evaluation_id + rule_versions + book_sequences + balance_versions + quantity + limits` 的执行授权。执行 worker 消费授权前重新拉取同步快照并完整复算；任一字段变化、ROI 跌破阈值或资金预留失败即废弃授权并等待下一次行情，不进入人工逐笔确认队列。详情页面只展示执行证据和状态，不提供“批准下单”按钮。

- [ ] **Step 3: 实现两腿提交与幂等恢复**

两腿使用同一 correlation ID 和各自稳定 client order ID，并发发送原生 FOK/等价即时订单。提交前最后检查 kill switch、模式、预留、规则哈希和行情年龄。未知响应不得盲目重下，必须按 client order ID 查询平台后恢复状态。

- [ ] **Step 4: 实现未对冲处置**

进入 `PARTIALLY_HEDGED` 后立即禁止全局新开仓，计算缺口数量和应急补对冲最坏成本。只有成本不超过预设最大损失时自动发送一次补对冲 FOK；否则尝试在已成交端按预设限价平仓并高优先级呼叫人工。超过最大未对冲时间后进入 `EXCEPTION`，不得静默继续。

- [ ] **Step 5: 在模拟端点完成故障注入并提交**

Run: `uv run pytest backend/tests/unit/services/test_execution_state_machine.py backend/tests/integration/services/test_execution_faults.py -q`

Expected: 故障矩阵全部通过；重复请求不会重复下单；每次状态变化都有不可变审计事件。

```powershell
git add backend/app/adapters backend/app/services backend/app/workers/execution.py backend/app/api frontend/src docs/runbooks backend/tests
git commit -m "feat: add controlled two-leg execution state machine"
```

### Task 12: 安全、可观测性、部署与紧急停机

**Files:**
- Create: `backend/app/core/security.py`
- Create: `backend/app/api/routes/system_control.py`
- Create: `observability/prometheus.yml`
- Create: `observability/alerts.yml`
- Create: `docs/runbooks/kill-switch.md`
- Create: `docs/runbooks/credential-rotation.md`
- Create: `docs/runbooks/reconciliation-failure.md`
- Create: `backend/tests/security/test_secret_redaction.py`
- Create: `backend/tests/integration/api/test_kill_switch.py`

- [ ] **Step 1: 隔离凭证与权限**

交易凭证只注入 execution worker；API、前端、discovery 和 market-data worker 不得读取。日志中对 `authorization`、API key、secret、private key、signature、cookie 自动脱敏。生产密钥来自云密钥库或操作系统密钥服务，不写 `.env`、数据库或审计 payload。

- [ ] **Step 2: 实现双层开关**

真实执行要求部署环境 `TRADING_MODE=limited_auto`，且数据库 `opening_enabled=true`。任一为 false 即拒绝新开仓；关闭开关不自动撤销或平仓，已有头寸进入异常处置 runbook，避免不可控的破坏性操作。

- [ ] **Step 3: 加入访问控制**

控制面仅允许本机访问，角色固定为 `viewer`、`reviewer`、`operator`。只有 reviewer 可审批映射，只有 operator 可修改限额或切换 kill switch；系统不提供逐笔人工批准接口。切换真实交易模式要求填写原因，单用户部署不强制双人审批。

- [ ] **Step 4: 配置告警和 SLO**

高优先级：任何未对冲头寸、对账差异、凭证失败、规则变更后仍有开放订单。中优先级：行情断流超过 30 秒、Oddpool 连续 5 次失败、通知堆积、费用偏差。只读系统目标为 99.5% 采集可用性；执行系统不以可用性换风控通过率。

- [ ] **Step 5: 验证与提交**

Run: `uv run pytest backend/tests/security/test_secret_redaction.py backend/tests/integration/api/test_kill_switch.py -q; uv run ruff check .; uv run mypy backend/app`

Expected: 秘密不出现在日志/错误响应；kill switch 在并发请求下仍阻止新提交；lint/typecheck 通过。

```powershell
git add backend/app/core backend/app/api observability docs/runbooks backend/tests
git commit -m "feat: harden operations and emergency controls"
```

### Task 13: 阶段验收与受限自动化开关

**Files:**
- Create: `docs/acceptance/read-only.md`
- Create: `docs/acceptance/shadow.md`
- Create: `docs/acceptance/canary-auto.md`
- Create: `docs/acceptance/limited-auto.md`
- Create: `backend/app/services/automation_gate.py`
- Test: `backend/tests/unit/services/test_automation_gate.py`

- [ ] **Step 1: 定义只读验收门槛**

连续 7 天无无法解释的序列缺口；规则变化能在一个采集周期内使映射失效；100% 报价可由证据重放；任意拒绝都有结构化原因；Oddpool 价格从不直接用于交易计算。

- [ ] **Step 2: 定义影子验收门槛**

至少连续 14 天且至少 100 次合格影子信号；在线与回放结果 100% 一致；零次使用过期 book；费用模型覆盖全部出现过的市场类别；将结果按市场类别拆分，不能让单个异常样本主导收益。

- [ ] **Step 3: 定义小额自动实盘门槛**

影子阶段通过后，只对白名单中的稳定 `EXACT` 映射自动执行，初始单笔上限固定为 `$10`、单事件上限 `$25`、总未结算资本 `$100`。整个交易链路没有逐笔人工确认。累计至少 30 次自动执行；每次实际费用与估算可解释；账户/订单/成交对账 100%；任何部分对冲均按 runbook 完整复盘。出现一次超过 `$2` 或配置上限的未对冲损失，立即关闭自动开仓、计数清零并重新评审状态机。

- [ ] **Step 4: 定义有限自动化门槛**

小额自动实盘通过后才允许提高配置限额，仍只对白名单 `EXACT` 映射开放，不增加任何逐笔人工步骤。提高限额要求连续 7 天无对账异常、最近 30 笔中至少 29 笔双腿完整成交、累计应急对冲/平仓损失不超过同期已配对净利润的 20%，且实际总净 PnL 和每美元资本占用日收益均为正。`automation_gate.py` 从数据库证据计算是否满足，任何指标缺失按失败处理。

- [ ] **Step 5: 全量验收与提交**

Run:

```powershell
uv run pytest -q
uv run ruff check .
uv run mypy backend/app
cd frontend
npm run build
npx playwright test
```

Expected: 后端测试、静态检查、前端构建和浏览器测试全部通过；自动化门槛不满足时 `LIMITED_AUTO` 无法开启。

```powershell
git add docs/acceptance backend/app/services/automation_gate.py backend/tests
git commit -m "feat: enforce evidence-based automation gates"
```

## 7. 实施节奏与人员配置

建议配置为 1 名后端/交易系统工程师、1 名前端/全栈工程师、0.5 名测试/运维支持，业务方每天能处理映射审核和规则确认。估算不包含等待账户开通、平台审核和积累交易样本的自然时间。

| 周期 | 交付 | 对应任务 | 退出条件 |
|---|---|---|---|
| 第 1 周 | 工程底座、领域模型、审计 | 1-3 | CI、迁移、状态图和 Decimal 测试通过 |
| 第 2 周 | Oddpool、原生元数据、映射审核 | 4-5 | 可从候选走到版本化 `EXACT/REJECTED` |
| 第 3-4 周 | 两端 L2、费用和数量优化 | 6-7 | 需求样例与 golden cases 可复算 |
| 第 5 周 | 只读工作台、通知 | 8 | 用户能看见机会、证据和拒绝原因 |
| 第 6 周 | 影子执行、历史回放 | 9 | 在线与回放一致，开始累计 14 天数据 |
| 第 7 周 | 余额、预留、对账 | 10 | 并发与断线测试无超额使用资金 |
| 第 8-9 周 | 审核后自动执行、安全运维 | 11-12 | 故障矩阵和安全检查通过 |
| 数据门槛后 | `$10` 小额自动实盘，再逐步提高限额 | 13 | 逐级满足验收门槛，不按日期强行扩额 |

若只有 1 名全栈工程师，预计开发周期约 12-14 周；影子观察至少另需 14 个自然日。真实自动化没有固定上线日期，只由 Task 13 的数据门槛决定。

## 8. 测试矩阵

| 测试层 | 必测内容 |
|---|---|
| Unit | Decimal/舍入、费用、逐档 VWAP、数量优化、状态转换、规则哈希、资本预留 |
| Contract | Oddpool、Kalshi、Polymarket 的脱敏真实响应 fixture；字段新增可容忍，关键字段缺失即失败 |
| Integration | PostgreSQL 事务、审计不可变、outbox 幂等、断线重同步、账户对账、kill switch |
| Fault injection | 超时、429、断流、乱序、重复回报、未知订单结果、单腿成交、进程崩溃、时钟偏差 |
| Replay | 任意历史决策可用当时证据逐字段重算，不读取未来数据 |
| E2E | 候选发现 -> 人工映射审核 -> 自动报价 -> 自动双腿提交 -> 成交/异常处置 |
| Security | 权限分离、凭证脱敏、CSRF/session、审计主体、交易 worker 独占凭证 |

CI 合并门槛：后端测试、ruff、mypy、前端 build、Playwright 全部通过；核心领域和执行状态机分支覆盖率至少 95%，仓库总覆盖率至少 85%。

## 9. MVP 成功标准

只读 MVP 不是“展示扫描器价差”，而是系统对每个候选都能回答并还原以下问题：

1. 两个合约为何被判定为 `EXACT/CONDITIONAL/REJECTED/STALE`？
2. 使用的是哪两个规则版本、哪两个原生订单簿序列和哪版费用规则？
3. 指定数量逐档会花多少钱，费用、缓冲、利润下界和保守 ROI 分别是多少？
4. 为什么当前允许或拒绝执行？
5. 两端余额、开放订单占用、预留资金和未结算资本是多少？
6. 下单后每条腿实际成交多少，未匹配数量是多少，异常如何处置？

系统不能完整回答其中任一项时，不允许进入自动交易阶段。

## 10. 方案自查

- 需求覆盖：ROI 自定义、结算期限、人工审核、审核后自动候选、按 ROI 排序、深度/滑点、费用、资金不足/充值提醒、扫描频率、双腿风险均已有任务和验收标准。
- 审阅覆盖：结算等价档案、Oddpool 降级、原生 L2、费率版本、双腿状态机、未对冲处置、预留资本、对账、密钥隔离、地域/条款、分阶段验证均已纳入。
- 一致口径：映射状态固定为 `PENDING_REVIEW/EXACT/CONDITIONAL/REJECTED/STALE`；执行状态固定为 `DISCOVERED/ELIGIBLE/PRECHECKED/SUBMITTED/PAIRED/PARTIALLY_HEDGED/EXCEPTION/CANCELLED`。
- 明确缺口：Oddpool 真实 contract、业务风控数值、通知渠道与交易凭证由业务方提供；缺失时系统保持 fixture、只读或影子模式，不用猜测值替代。
