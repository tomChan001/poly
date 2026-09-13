# 审核页时间与市场信息 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在审核页完整展示候选的关键时间和所有对审核有帮助的市场字段。

**Architecture:** 补齐 `ExecutablePair` 前端类型，在 `PairSettingsPage` 内集中格式化时间和中文状态，并以两个只读信息区展示。继续复用现有 `/api/pairs` 响应，不修改后端或数据库。

**Tech Stack:** React、TypeScript、Vitest、Testing Library、Playwright。

---

### Task 1: 字段契约与页面展示

**Files:**
- Modify: `frontend/src/types/runtime.ts`
- Modify: `frontend/src/pages/PairSettingsPage.tsx`
- Modify: `frontend/src/App.css`
- Modify: `frontend/src/App.test.tsx`

- [ ] **Step 1: 写失败的展示测试**

给审核候选 fixture 增加三种结算时间、市场类别、最小价格单位和内部指纹。断言页面显示四个时间标签、北京时间结果、空时间的“平台暂未提供”、最小数量、数量递增单位、市场类别、价格单位、启用状态和审核人状态。

- [ ] **Step 2: 运行测试确认失败**

Run: `npm --prefix frontend test -- src/App.test.tsx`
Expected: FAIL，因为审核页尚未渲染这些字段。

- [ ] **Step 3: 补齐类型并实现展示**

在 `ExecutablePair` 中声明后台已有字段；在审核标题与规则对比之间加入“关键时间”和“市场信息”。使用 `Intl.DateTimeFormat` 和 `Asia/Shanghai` 转换有效时间，对空值与无效值给出明确文本。

- [ ] **Step 4: 运行前端验证**

Run: `npm --prefix frontend run lint`
Expected: PASS。

Run: `npm --prefix frontend test`
Expected: PASS。

Run: `npm --prefix frontend run build`
Expected: PASS。

- [ ] **Step 5: 运行响应式验证**

Run from `frontend`: `npx playwright test tests/runtime.spec.ts`
Expected: desktop 和 mobile 两个场景 PASS，页面无横向溢出。
