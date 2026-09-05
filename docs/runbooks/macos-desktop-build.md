# macOS 桌面版构建、安装与恢复

Poly 桌面版把 Web 前端、Python 3.12 运行时和 PostgreSQL 16 一起封装进每个架构专用的 `.dmg`。运营员只安装 Poly，不需要 Python、Node.js、PostgreSQL、Docker、Homebrew 或其他运行时，也不需要 AWS 运行时配置。

## CI 模式与产物

`.github/workflows/macos-desktop.yml` 使用原生架构 runner，绝不生成 universal binary：

| Runner | Rust target | GitHub Actions 产物 |
| --- | --- | --- |
| `macos-15` | `aarch64-apple-darwin` | `Poly-macos-arm64` |
| `macos-15-intel` | `x86_64-apple-darwin` | `Poly-macos-x86_64` |

Pull request 和手动 `workflow_dispatch` 只执行 unsigned/ad-hoc validation。它们运行冻结依赖测试、原生 PostgreSQL/PyInstaller/Tauri 构建和隔离安装 smoke，但不产生可发布的签名身份声明。

只有匹配 `v*` 的显式 release tag push 才进入 Developer ID 签名、公证和 staple 流程。Tag 构建缺少任意 Apple secret 时会失败，不会降级为貌似可发布的 DMG。此 workflow 不上传到应用商店、服务器或云运行时；产物只保存在对应 Actions run 的 Artifacts 区域。

Intel 构建已配置，但在 `macos-15-intel` 的完整绿色原生 CI 运行出现前，不得把 Intel DMG 标为受支持或向运营员发布。Windows 上的测试不能证明 macOS App/DMG 已生成、签名、公证或可运行。

## Apple Developer 前置条件

发布人员需要有效的 Apple Developer Program membership、适用于该 Team 的 `Developer ID Application` certificate，以及可提交 notarization 的 Apple ID 凭据。在 GitHub repository secrets 中配置以下全部名称。当前 workflow 没有声明 GitHub Environment，因此不要把这些值只配置成 environment secrets：

- `APPLE_CERTIFICATE`：Developer ID Application `.p12` 的 base64 内容。
- `APPLE_CERTIFICATE_PASSWORD`：导出 `.p12` 时设置的密码。
- `KEYCHAIN_PASSWORD`：CI 临时 keychain 的随机强密码。
- `APPLE_SIGNING_IDENTITY`：完整 identity，例如 `Developer ID Application: Company Name (TEAMID)`。
- `APPLE_ID`：提交公证使用的 Apple ID。
- `APPLE_PASSWORD`：该 Apple ID 的 app-specific password。
- `APPLE_TEAM_ID`：Apple Developer Team ID。

不要把这些值写入 workflow、日志、issue、artifact 或本地 `.env`。CI 仅在 release tag 的签名步骤中使用它们；证书进入临时 keychain，`always()` cleanup 会删除临时 keychain 和证书文件。Repository secrets 必须限制为受信任维护者可管理，release tag 创建权限也必须受保护。

## 验证与发布操作

1. 从 Actions 页面选择 `macOS desktop validation`，运行 `workflow_dispatch`。这是 unsigned/ad-hoc validation，不是发布批准。
2. 检查 Apple Silicon 矩阵 job；若计划发布 Intel，还必须检查 Intel job 的测试、bundle 校验和隔离 smoke 均为绿色。
3. 创建并 push 经审核的 `v*` release tag。保护的 Apple secrets 必须完整，两个 job 才会生成经过 Developer ID 签名、公证和 staple 的 release DMG。
4. 从该 tag 对应 run 下载 `Poly-macos-arm64`，以及仅在 Intel 原生 job 有绿色证据时下载 `Poly-macos-x86_64`。核对下载来源、run、commit 和架构；不要重命名 validation artifact 冒充 release。

本 workflow 不连接真实市场服务，不使用真实市场凭证，也不执行真实订单。Python CI 只运行使用 fake/mock transports 的 unit 和 security 测试。

隔离 smoke 使用唯一的合成值和临时默认 Keychain，并通过 Poly 的生产 `KeyringSecretStore` 边界执行 set/get/delete；它在两次应用启动之间读取同一值，结束时删除测试项和临时 Keychain。这个测试不会向正式应用增加调试 endpoint 或 secret readback，也不会修改发布 artifact。Smoke 同时使用 PATH deny shim 和应用后代进程采样，拒绝从 `.app` 外调用 `python`、`python3`、`node`、`postgres`、`aws`、`docker` 或 `brew`，并只允许 bundle 内可执行文件和明确允许的 macOS 系统路径。Validation 即使失败也通过独立的 `always()` step 上传不含凭证值的工具链、签名检查、bundle 清单和进程审计诊断。

## 安装与首次启动

1. 根据 Mac 架构打开对应的已公证 DMG。
2. 将 `Poly.app` 拖到 `/Applications`，再从 Applications 启动。
3. 首次保存 Kalshi、Polymarket 或 Oddpool 凭证时，macOS 可能显示标准 Keychain 授权提示。确认应用是已验证的 Poly 后再授权；Poly 使用稳定 service name `com.poly.desktop.integrations`，升级后仍读取同一组授权。

运营员无需安装任何其他运行时。应用只在本机回环地址提供 UI/API，PostgreSQL 只使用本地 Unix socket。没有 AWS runtime configuration，也不要为桌面版增加 AWS 凭证。

## 数据、日志与停止状态备份

持久数据位于 `~/Library/Application Support/Poly`，诊断日志目录位于 `~/Library/Application Support/Poly/logs`，PostgreSQL 的独立日志文件位于 `~/Library/Application Support/Poly/postgres.log`。运行缓存位于 `~/Library/Caches/com.poly.desktop`。市场凭证保存在 macOS Keychain，不在数据目录或备份中。

备份必须在 stopped state 完成：

1. 在菜单中退出 Poly，并在 Activity Monitor 确认 Poly、打包的 `poly-runtime` 和它拥有的 PostgreSQL 子进程均已停止。
2. 将 `~/Library/Application Support/Poly` 完整复制到受控备份位置并保留权限和时间戳，例如：

   ```bash
   ditto "$HOME/Library/Application Support/Poly" "/Volumes/Backup/Poly-$(date +%Y%m%d-%H%M%S)"
   ```

3. 单独按组织的凭证恢复流程管理 Keychain；不要把真实凭证导出到该目录。

## 故障恢复

- 迁移失败：保持 Poly 退出，先制作停止状态备份，再查看 `logs`。不要反向修改 schema；恢复已验证备份或安装修复版本后再启动。
- Keychain 拒绝：在 System Settings/Keychain Access 核对 Poly 的授权和 Developer ID；不要把凭证改存为文本文件。重新授权后使用错误页的“重试”。
- Runtime/resource 错误：从同一可信 release 重新安装对应架构的 `Poly.app`，不要删除 `Application Support/Poly`。先查看日志，再重试。
- 重试仍失败：退出 Poly，保留数据目录与日志副本，记录应用版本、Mac 型号、架构和 Actions release run，然后升级处理。恢复数据时必须保持 Poly 停止，先把当前故障目录另存，再用已验证备份替换。

Windows 仅能执行可移植的 Python、frontend 和 Rust 检查；它不能提供原生签名、公证、Gatekeeper、Keychain、DMG 安装或退出清理证据。最终发布判断必须来自对应架构的绿色 macOS CI 和真实 Mac smoke。
