# Kalshi API Key Guidance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add official, security-conscious Kalshi Key ID and RSA private-key acquisition instructions to the integration page and README.

**Architecture:** Extend the existing provider-specific help block in `IntegrationSettingsPage` rather than adding a new component or API. Keep the RSA private key in the existing password field and operating-system credential store; documentation changes only explain how to obtain and paste it.

**Tech Stack:** React, TypeScript, Testing Library/Vitest, Markdown

---

### Task 1: Kalshi configuration-page guidance

**Files:**
- Modify: `frontend/src/pages/IntegrationSettingsPage.tsx`
- Test: `frontend/src/App.test.tsx`

- [ ] **Step 1: Write the failing rendering test**

Add these assertions to the existing integration-settings test after the Kalshi endpoint/environment assertions:

```tsx
expect(
  screen.getByRole('link', { name: 'Kalshi 官方 API Key 获取说明' }),
).toHaveAttribute('href', 'https://docs.kalshi.com/getting_started/api_keys')
expect(screen.getByText(/Account & security → API Keys/)).toBeInTheDocument()
expect(screen.getByText(/私钥只显示和下载一次/)).toBeInTheDocument()
expect(
  screen.getByText(/API Key ID 填入 Key ID；下载的 .key 文件完整内容填入 RSA 私钥/),
).toBeInTheDocument()
expect(screen.getByLabelText('RSA 私钥')).toHaveAttribute('type', 'password')
```

- [ ] **Step 2: Run the test and confirm RED**

Run:

```powershell
cd frontend
npm run test -- --run src/App.test.tsx
```

Expected: FAIL because the Kalshi official link and guidance text are not rendered.

- [ ] **Step 3: Add the minimal Kalshi help block**

In `IntegrationSettingsPage`, immediately before the Polymarket-specific block, add:

```tsx
{definition.provider === 'kalshi' && (
  <div className="field-wide integration-help">
    <p>
      登录 Kalshi，进入 <strong>Account &amp; security → API Keys</strong>，
      点击 <strong>Create Key</strong>。
    </p>
    <p>
      API Key ID 填入 Key ID；下载的 .key 文件完整内容填入 RSA 私钥。
      请粘贴包含 BEGIN/END PRIVATE KEY 的完整 PEM，不要填写文件路径。
    </p>
    <p>私钥只显示和下载一次，关闭页面前请安全保存。</p>
    <a
      href="https://docs.kalshi.com/getting_started/api_keys"
      target="_blank"
      rel="noreferrer"
    >
      Kalshi 官方 API Key 获取说明
    </a>
  </div>
)}
```

Reuse the existing `field-wide` layout behavior and add no new CSS; the current integration help block already establishes the required paragraph/link spacing.

- [ ] **Step 4: Run the focused test and confirm GREEN**

Run:

```powershell
cd frontend
npm run test -- --run src/App.test.tsx
```

Expected: the test file passes with the new assertions.

- [ ] **Step 5: Commit the page and test**

```powershell
git add frontend/src/pages/IntegrationSettingsPage.tsx frontend/src/App.test.tsx
git commit -m "feat: explain kalshi api credentials"
```

### Task 2: README instructions and final verification

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add the detailed README section**

Insert this section before `### Polymarket Google / 邮箱账户`:

```markdown
### Kalshi API 凭证

1. 登录与当前环境匹配的 Kalshi 生产或 Demo 账户。
2. 进入 `Account & security → API Keys`，点击 `Create Key`。
3. 将页面显示的 API Key ID 填入本项目的 `Key ID`。
4. 打开下载的 `.key` 文件，将包含 `-----BEGIN PRIVATE KEY-----` 和
   `-----END PRIVATE KEY-----` 的完整 PEM 内容粘贴到 `RSA 私钥`。不要填写文件名或路径。

私钥只显示和下载一次，Kalshi 不会保留可供再次下载的副本。Key ID 不是文件名、登录密码或私钥内容。创建替代密钥时，必须同时更新新 Key ID 和对应的新私钥。详见 [Kalshi 官方 API Key 文档](https://docs.kalshi.com/getting_started/api_keys)。

如果连接测试返回 401，检查 Key ID 与私钥是否来自同一次创建、生产/Demo 环境是否匹配，以及 PEM 头尾是否完整。
```

- [ ] **Step 2: Verify documentation and frontend quality gates**

Run:

```powershell
rg -n "Kalshi API 凭证|Account & security|BEGIN PRIVATE KEY|docs.kalshi.com/getting_started/api_keys" README.md frontend/src/pages/IntegrationSettingsPage.tsx
cd frontend
npm run test
npm run lint
npm run build
```

Expected: both files contain the official guidance; all frontend tests pass; lint and production build exit successfully.

- [ ] **Step 3: Visually verify the integration page**

Run the existing Playwright suite:

```powershell
cd frontend
npm exec playwright test
```

Expected: desktop and mobile integration-settings tests pass without overflow or clipped guidance.

- [ ] **Step 4: Commit the README**

```powershell
git add README.md
git commit -m "docs: explain kalshi api key setup"
```
