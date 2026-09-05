# Poly 本地 macOS 桌面客户端设计

日期：2026-09-04

状态：待产品审核

目标平台：macOS 12 及以上，Apple Silicon 优先，Intel 单独构建

## 1. 决策摘要

Poly 改为单机桌面产品。最终用户只安装并启动 `Poly.app`，无需 AWS、EC2、SSM、Docker、Homebrew、Python、Node.js、PostgreSQL、命令行工具或管理员权限。应用退出后，不保留任何后台交易进程或本地监听端口。

桌面包包含四部分：

1. Tauri 2 原生外壳，负责窗口、状态展示、进程监督和退出清理。
2. 现有 React 前端的生产构建。
3. 打包后的 Python/FastAPI 运行时，包含现有 worker 和 Alembic 迁移。
4. 每个 CPU 架构对应的 PostgreSQL 16 本地二进制。

运行时仅监听随机的 `127.0.0.1` 端口，PostgreSQL 仅使用权限受限的 Unix socket。市场平台凭证继续由后端写入 macOS Keychain，不进入前端、数据库、配置文件或日志。

本设计取代此前的 AWS SSM 隧道方案。桌面版本不包含 AWS SDK、AWS CLI、Session Manager Plugin、IAM Identity Center 配置或任何 AWS 资源定义，也不会产生云费用。

## 2. 目标与非目标

### 2.1 目标

- 用户从 DMG 将 Poly 拖入 Applications 后即可双击使用。
- 首次启动自动初始化本地数据库并完成迁移，不出现终端窗口。
- 后续启动复用本地数据和 Keychain 凭证。
- React 页面显示在 Tauri WebView 中，保留现有业务交互。
- 启动、故障恢复、重启和退出都有明确状态。
- 关闭应用时停止下单入口、收敛正在进行的操作并终止所有子进程。
- 产物可使用 Developer ID 签名、公证并生成 DMG。
- 所有外部运行依赖随 App 一起分发。

### 2.2 非目标

- 不支持远程访问、多人共享、后台常驻或退出后继续交易。
- 不提供公网 Web 入口。
- 不把 PostgreSQL 改成 SQLite。
- 首个版本不实现自动更新；升级通过新的已签名 DMG 完成。
- 不在本任务中创建、修改或部署任何云资源。

## 3. 用户体验

### 3.1 安装与首次启动

1. 操作员打开已签名并完成 notarization 的 DMG。
2. 将 Poly 拖入 Applications，也可固定到 Dock。
3. 双击 Poly。
4. 窗口依次显示“正在准备本地数据”“正在升级数据库”“正在启动服务”。
5. 服务就绪后，同一窗口自动进入现有 React 页面。
6. 当操作员首次配置 Kalshi、Polymarket 或 Oddpool 时，凭证写入 macOS Keychain。系统可能显示一次标准 Keychain 授权提示。

整个流程不要求用户打开终端或填写端口、实例 ID、数据库地址等技术参数。

### 3.2 后续启动

应用打开现有数据目录，验证数据库版本，执行必要的向前迁移，然后启动 Web 服务和 worker。市场凭证按现有后端逻辑从 Keychain 获取，前端只能看到配置状态或不可逆指纹。

### 3.3 退出

用户关闭主窗口或选择退出时：

1. 原生外壳进入“正在安全退出”状态并禁止新的 UI 操作。
2. 后端关闭新的开仓入口，并在有限期限内收敛正在执行的业务操作。
3. worker 停止，FastAPI 停止接受请求。
4. PostgreSQL 执行快速但一致的关闭。
5. Unix socket、临时目录和随机 HTTP 监听端口消失。
6. 超时后仅终止属于本次启动、且身份已验证的子进程，绝不根据陈旧 PID 随意杀进程。

若进程崩溃或被强制结束，下一次启动先进行数据库恢复和未完成操作核对，再允许新的开仓操作。

## 4. 进程与模块架构

```text
Poly.app
└── Tauri 2 原生外壳
    ├── 状态窗口 / WebView
    ├── RuntimeSupervisor
    │   └── poly-runtime（PyInstaller onedir sidecar）
    │       ├── PostgreSQL 16 进程
    │       ├── Alembic 迁移
    │       ├── FastAPI / Uvicorn
    │       └── 现有后台 worker
    └── 打包资源
        ├── React dist
        ├── PostgreSQL 二进制和共享库
        └── 第三方许可证清单
```

### 4.1 Tauri 原生外壳

Tauri 负责：

- 保证同一用户只运行一个 Poly 桌面实例。
- 创建应用数据目录和权限受限的临时目录。
- 启动、监督、重启和停止 `poly-runtime` sidecar。
- 接收结构化状态事件并驱动原生启动/故障页面。
- 服务就绪后把 WebView 导航到随机 loopback 地址。
- 执行有界的优雅退出和经过身份验证的兜底清理。

连接逻辑、认证能力令牌、生命周期管理和 WebView 展示分别放在独立 Rust 模块中，避免把业务状态与窗口事件耦合。

### 4.2 Python/FastAPI sidecar

使用 PyInstaller `onedir` 模式为每种 macOS 架构构建 `poly-runtime`。它包含 CPython、Python 依赖、后端源码、Alembic 和前端静态资源，因此目标 Mac 不需要安装 Python。

选择 `onedir` 而不是每次启动自解压的单文件模式，原因是：

- 启动时间更稳定。
- 原生扩展和共享库更容易签名与审计。
- 不需要把大量可执行内容解压到临时目录。
- 故障诊断和第三方许可证归档更清晰。

Tauri 只直接启动一个 `poly-runtime`。PostgreSQL 由该 sidecar 启动和管理，Tauri 不拼接数据库密码或命令行参数。

### 4.3 PostgreSQL sidecar

保留 PostgreSQL，而不切换到 SQLite。当前代码依赖 JSONB、事务级 advisory lock、`ON CONFLICT`、审计触发器和并发语义，数据库替换会扩大正确性风险。

应用固定 PostgreSQL 16 大版本，每个 CPU 架构单独编译和测试。数据放在：

```text
~/Library/Application Support/Poly/postgres/data
```

运行规则：

- `listen_addresses=''`，禁止 PostgreSQL TCP 监听。
- Unix socket 放在 `$TMPDIR` 下本次启动创建的 `0700` 随机目录，避免 Unix socket 路径长度限制。
- 首次启动执行 `initdb`，只开放当前 macOS 用户可访问的本地 socket。
- 所有迁移完成前不启动 worker，也不把 UI 标为就绪。
- 同一 PostgreSQL 大版本内允许应用迁移；未来升级大版本必须提供显式备份和 `pg_upgrade` 路径，不能用新版本直接打开旧数据目录。

## 5. 启动协议和生命周期

### 5.1 父子进程控制通道

Tauri 创建 sidecar 后，通过其标准输入发送一次 JSON 启动消息，其中包含：

- 应用数据目录和本次启动的临时目录。
- 256 位随机启动令牌。
- 桌面运行模式和协议版本。

这些值不放进命令行参数，避免出现在进程列表中；也不写入持久配置。标准输入保持打开，作为父进程存活租约。Tauri 正常退出会发送结构化 shutdown 消息；Tauri 崩溃导致管道 EOF 时，sidecar 也进入关闭流程。

sidecar 通过标准输出发送逐行 JSON 事件，例如：

```json
{"version":1,"state":"starting_database"}
{"version":1,"state":"migrating","revision":"..."}
{"version":1,"state":"ready","port":49152,"bootstrap_path":"/desktop/bootstrap/..."}
```

标准输出只承载控制协议。诊断信息写到经过脱敏和轮转的本地日志，不记录凭证、启动令牌、会话 capability 或完整请求 URL。冻结的 sidecar 还会验证直接父进程位于同一 bundle，并校验父进程与自身的签名 Team ID；任意终端或同用户进程不能直接启动生产控制面。

### 5.2 HTTP 端口分配

sidecar 自己让操作系统在 `127.0.0.1:0` 分配端口，并持有该监听 socket 后再启动 Uvicorn，然后把实际端口报告给 Tauri。这样避免“先找空闲端口、释放、再绑定”的竞争窗口。

禁止绑定 `0.0.0.0`、局域网地址或 IPv6 wildcard。React 静态资源和 API 由同一 FastAPI origin 提供，现有相对路径 `/api` 和 `/health` 可继续工作。

### 5.3 WebView 引导能力

loopback 绑定仍然是首要边界，并保留现有 `backend/app/core/security.py` 的 localhost、loopback 和可信 Origin 校验，不降低或绕过这些检查。

桌面模式另外增加一次性能力引导：

1. sidecar 根据启动令牌生成一次性高熵 bootstrap 路径。
2. Tauri 仅把 WebView 导航到该路径一次。
3. 后端验证后把独立随机会话 capability 放入 303 目标的 URL fragment；fragment 不会随 HTTP 请求发送。React 启动代码把它保存到当前精确 origin（包含随机端口）隔离的 `sessionStorage` 和内存中，并立即通过 `history.replaceState` 从地址栏清除；这样 WebView reload 后仍能恢复会话，而另一个本地端口无法读取。
4. 桌面模式下，业务 API 在现有安全检查之外要求显式 `X-Poly-Desktop-Session` header，并把 Host/Origin 精确绑定到本次随机端口。
5. Uvicorn 访问日志关闭，因此一次性路径不会进入日志。

会话不使用按主机共享、忽略端口的 ambient Cookie。capability 只存在于当前随机端口的 origin-scoped `sessionStorage`、进程内存和极短暂的 WebView fragment 中；随机端口、精确 authority 校验、禁止 framing 和父进程签名验证共同保护本地控制面。

`/health` 对未认证请求只返回无敏感信息的存活状态；完整运行状态只通过已认证 API 或父子控制通道暴露。

## 6. 安全边界

### 6.1 威胁模型

假设操作员可信，macOS 用户账户和系统未被恶意软件或 root 权限攻击者控制。设计保护：

- 不向局域网或公网暴露应用端口。
- 降低普通网页对本地 API 的跨站请求风险。
- 防止凭证进入 React、数据库、配置、日志和崩溃报告。
- 防止陈旧 PID 导致误杀无关进程。
- 防止应用退出后意外保留交易 worker 或监听端口。

设计不声称能抵御同一用户权限下可读取进程内存、注入进程或控制 Keychain 的恶意程序，也不能抵御 root。

### 6.2 市场平台凭证

沿用现有 `KeyringSecretStore`，在 macOS 上使用 Keychain。服务标识必须稳定，例如 `com.poly.desktop.integrations`，以便升级版本继续访问同一凭证并保持可控的 Keychain ACL。

- Kalshi、Polymarket 和 Oddpool 凭证不存入 PostgreSQL。
- 前端只接收“已配置/未配置”和不可逆指纹。
- 后端仅在连接测试或业务调用需要时读取凭证，并尽快释放引用。
- 错误消息、结构化状态、日志和遥测统一经过脱敏。
- 首发版本默认不上传崩溃报告或遥测。
- 稳定的 bundle identifier 与签名身份用于减少升级时重复的 Keychain 授权提示。

### 6.3 文件权限

应用数据目录及其敏感子目录只允许当前用户访问。运行锁包含本次启动随机标识、可执行文件路径摘要和协议版本；清理进程前同时验证父子关系、路径和启动标识，不能只信任 PID 文件。

不安装 LaunchDaemon、LaunchAgent、内核扩展或系统级服务，不申请管理员权限。

## 7. 状态机与错误恢复

原生状态界面至少支持：

| 状态 | 用户文案 | 自动动作 |
| --- | --- | --- |
| `initializing` | 正在准备 Poly | 校验目录和包资源 |
| `preparing_database` | 正在准备本地数据库 | 初始化或恢复 PostgreSQL |
| `migrating` | 正在升级本地数据 | 执行 Alembic |
| `starting_services` | 正在启动服务 | 启动 FastAPI 和 worker |
| `ready` | 已连接到本地服务 | 显示 React 页面 |
| `restarting` | 服务中断，正在重启 | 指数退避后重启 |
| `keychain_denied` | 无法访问系统钥匙串 | 提供重试和说明 |
| `data_directory_error` | 无法访问本地数据 | 显示可操作的路径错误 |
| `migration_failed` | 本地数据升级失败 | 停止 worker，保留数据，提供日志位置 |
| `runtime_unavailable` | 本地服务无法启动 | 有界重试或人工重试 |
| `shutting_down` | 正在安全退出 | 收敛业务操作并停止进程 |

可恢复的 runtime 崩溃采用带抖动的指数退避并设置重启上限。迁移失败、资源缺失、签名损坏或确定性配置错误不无限重试，而是保持错误页并提供“重试”和“在 Finder 中显示诊断日志”。

发生非正常退出时写入不含秘密的 `unclean_shutdown` 标记。下次启动在 worker 开放新交易前执行未完成操作核对；核对失败时保持只读/禁止开仓状态，并清楚提示操作员。

## 8. 数据、备份与升级

持久数据根目录为：

```text
~/Library/Application Support/Poly/
├── postgres/data/
├── logs/
├── runtime-state.json
└── version.json
```

凭证不在此目录中，而在 Keychain。

应用升级遵循以下规则：

- 替换 `/Applications/Poly.app` 不删除 Application Support 数据。
- 每次启动迁移前记录应用版本、数据库 schema 版本和 PostgreSQL 大版本。
- 迁移只能向前，失败后禁止 worker 启动。
- 在需要破坏性迁移或 PostgreSQL 大版本升级前，新增“应用已停止状态下导出/备份”的正式流程。
- 首发版本使用手动下载的新签名 DMG，不引入自动更新服务或新的网络信任边界。

## 9. 构建、签名和分发

### 9.1 架构产物

分别构建：

- `Poly-macos-arm64.dmg`，首要支持 Apple Silicon。
- `Poly-macos-x86_64.dmg`，在成本可接受且 CI 验证通过时提供 Intel 版本。

不首选 universal2 单包，因为 Python 原生扩展、PostgreSQL 和第三方动态库需要一致的双架构构建，两个独立 DMG 更容易验证，也不要求 Apple Silicon 用户安装 Rosetta。

### 9.2 macOS CI

Windows 开发机只能执行可移植的前端、Python 和 Rust 单元测试，不能声称已经生成或验证 macOS App/DMG。macOS CI 按目标架构执行：

1. 安装锁定版本的 Node、Rust 和构建期 Python。
2. 构建 React 生产资源。
3. 使用 PyInstaller onedir 构建对应架构的 `poly-runtime`。
4. 构建或取得可审计、版本固定的 PostgreSQL 16 对应架构二进制。
5. 收集 npm、Cargo、Python、CPython、PostgreSQL 及原生库许可证，生成 `THIRD_PARTY_NOTICES`。
6. 按 Tauri target triple 命名并纳入 external binary/resource。
7. 由内到外签名嵌套可执行文件、Python 扩展和 dylib，启用 hardened runtime。
8. 签名 `.app` 和 DMG，提交 Apple notarization 并 staple 票据。
9. 执行 `codesign --verify --deep --strict`、`spctl --assess` 和 `xcrun stapler validate`。
10. 在干净 Mac 环境挂载 DMG、复制到 Applications、首次启动、重启和退出，验证无外部依赖、无残留端口和子进程。

Apple 签名证书、notarization 凭证等只存放在 CI 的受保护秘密存储中，不进入仓库或构建日志。

## 10. 测试策略

实现采用测试驱动方式，所有外部平台调用都可 mock，测试不访问真实市场账户或 AWS。

### 10.1 Rust/Tauri

- supervisor 状态转换和非法转换。
- sidecar JSON 协议解析、版本不匹配和输出污染。
- EOF、正常退出、超时、崩溃和有界重启。
- 子进程身份验证和陈旧 PID 防护。
- 随机端口/引导完成后才导航 WebView。
- 退出顺序和强制清理边界。

### 10.2 Python/FastAPI

- 桌面启动消息解析且秘密不进入参数、环境和日志。
- socket 绑定到 `127.0.0.1`，拒绝 wildcard 配置。
- 一次性 bootstrap 只能成功一次，React 读取 fragment 后立即清除地址栏令牌。
- capability header 缺失、错误、跨端口或来源不匹配时拒绝业务 API。
- 现有 localhost/loopback/Origin 安全测试保持通过。
- PostgreSQL 初始化、迁移失败和恢复逻辑使用临时目录/受控进程替身测试。
- Keychain 层 mock，验证数据库与日志中不出现凭证。
- 非正常退出后的交易核对门禁。

### 10.3 React

- 每个启动和错误状态的文案与可操作按钮。
- 就绪后加载现有页面。
- runtime 断开时退出业务页面并显示重启状态。
- 重连成功后恢复页面，不能绕过安全门禁。

### 10.4 macOS 端到端

- 新用户首次安装和启动，无 Python/PostgreSQL/Homebrew/AWS CLI。
- Keychain 首次写入、后续读取和用户拒绝场景。
- 正常退出、强制结束 Tauri、强制结束 runtime、系统重启后的恢复。
- 同时启动两个 App 实例。
- 活跃请求期间退出并在下次启动执行核对。
- Apple Silicon 必测；Intel 在发布 Intel DMG 前必测。

## 11. 实施边界与模块拆分

建议按以下边界实施：

- `frontend/src/desktop/`：启动、重连、错误和安全退出界面。
- `src-tauri/src/runtime/`：sidecar 协议与 supervisor。
- `src-tauri/src/webview/`：引导 URL 和页面切换。
- `src-tauri/src/app_lifecycle/`：单实例、退出和崩溃清理。
- `backend/app/desktop/`：启动协议、能力会话和本地资源编排。
- `packaging/macos/`：PyInstaller spec、PostgreSQL 构建、许可证和签名脚本。
- `.github/workflows/`：macOS 构建、测试、签名、公证和 smoke test。

现有 FastAPI 安全模块保持原有语义。桌面能力校验作为额外依赖组合进去，不能删除或放宽 `backend/app/core/security.py` 中的 loopback 与 Origin 检查。

## 12. 备选方案及取舍

### 12.1 要求用户安装 Python/PostgreSQL/Homebrew

拒绝。它违背单一 App 安装体验，且版本、PATH、服务启动和权限问题会直接落到操作员身上。

### 12.2 内置 Docker 或轻量虚拟机

拒绝。包体、启动时间、权限、网络和签名复杂度远高于直接内置 sidecar。

### 12.3 改用 SQLite

拒绝用于本阶段。当前 PostgreSQL 特性与并发正确性具有业务价值，迁移成本和回归风险高于打包 PostgreSQL 的工程成本。

### 12.4 保留 AWS SSM 隧道

拒绝。单用户本地运行不需要远端服务器或身份登录，保留它只会增加云费用、认证和故障面。

## 13. 发布验收标准

首个 Apple Silicon 发布候选必须满足：

- 干净的 macOS 12+ 机器只安装 Poly.app 即可启动。
- 不存在 AWS、Docker、Homebrew、Python、Node 或 PostgreSQL 的运行时前置要求。
- React、FastAPI、worker 和 PostgreSQL 均在应用监督下本地运行。
- PostgreSQL 不监听 TCP，FastAPI 只监听随机 `127.0.0.1` 端口。
- 现有 loopback/Origin 检查全部保留并通过测试。
- 凭证只存在于 Keychain，敏感值不进入数据库、文件和日志。
- 正常退出和模拟崩溃后无残留 Poly 子进程或监听端口。
- DMG 通过签名、公证、staple 和 Gatekeeper 验证。
- 在真实 Apple Silicon Mac 上完成安装、首次启动、重启和退出 smoke test。
- 若提供 Intel DMG，则在真实或可信 Intel macOS 环境重复同等验证。

## 14. 官方依据

- [Tauri 2 Sidecar 文档](https://v2.tauri.app/develop/sidecar/)：支持随应用打包外部二进制，并按 target triple 提供平台产物。
- [Tauri macOS 签名文档](https://v2.tauri.app/distribute/sign/macos/)：描述 Developer ID、签名与 notarization 流程。
- [PyInstaller 官方手册](https://pyinstaller.org/en/stable/)：打包 Python 应用及依赖，使目标机器不必安装 Python；产物需在目标操作系统构建。
- [PostgreSQL License](https://www.postgresql.org/about/licence/)：允许在保留版权与许可声明的前提下使用和再分发 PostgreSQL。
- [Apple Keychain Services](https://developer.apple.com/documentation/security/keychain-services)：提供系统级安全存储与访问控制。
