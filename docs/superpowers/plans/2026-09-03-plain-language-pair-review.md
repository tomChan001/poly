# 清晰易懂的市场对审核页 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将市场对审核页改成易懂的确认提示，并清楚解释为什么十项全部核对后才可同意。

**Architecture:** 仅调整 `PairSettingsPage` 的展示数据和确认条件，继续使用既有 `reviewPair` 接口保存完整 checklist。页面根据未勾选数量显示提醒，并与后台既有的完整 checklist 校验保持一致。

**Tech Stack:** React、TypeScript、Vitest、Testing Library。

---

### Task 1: 审核页测试与行为

**Files:**
- Modify: `frontend/src/App.test.tsx`
- Modify: `frontend/src/pages/PairSettingsPage.tsx`
- Modify: `frontend/src/App.css`

- [ ] **Step 1: 写失败的 UI 测试**

在既有审核测试中，保留一个未勾选项，断言出现“还有 1 项未核对”的提醒和必须全部核对的原因，且 `确认可以配对` 按钮不可用；最后一项勾选后按钮可用并正常提交。

- [ ] **Step 2: 运行测试确认失败**

Run: `npm --prefix frontend test -- src/App.test.tsx`
Expected: FAIL，因为页面尚未显示必须全部核对的通俗原因和新的完成提示。

- [ ] **Step 3: 实现最小页面变更**

将技术标签映射为简明的“核对内容 / 为什么要看”文案；增加审核目的、遗漏数量和结算说明。保留确认按钮的完整 checklist 禁用条件，并保留提交时所有 checklist 键和值的保存。

- [ ] **Step 4: 运行聚焦测试**

Run: `npm --prefix frontend test -- src/App.test.tsx`
Expected: PASS。

- [ ] **Step 5: 运行构建验证**

Run: `npm --prefix frontend run build`
Expected: PASS。
