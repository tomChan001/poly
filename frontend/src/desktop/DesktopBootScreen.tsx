import { useState } from 'react'

import './desktop.css'

/* oxlint-disable react/only-export-components -- Query parsing lives with its allowlisted desktop state contract. */

export const DESKTOP_UI_STATES = [
  'initializing',
  'preparing_database',
  'migrating',
  'starting_services',
  'restarting',
  'keychain_denied',
  'migration_failed',
  'runtime_unavailable',
  'permission_denied',
  'resource_missing',
  'protocol_failed',
  'shutting_down',
] as const

export type DesktopUiState = (typeof DESKTOP_UI_STATES)[number]

type DesktopAdapter = {
  retry: () => void | Promise<void>
  revealLogs: () => void | Promise<void>
}

declare global {
  interface Window {
    __POLY_DESKTOP__?: DesktopAdapter
  }
}

type DesktopBootScreenProps = {
  state: DesktopUiState
  canRevealLogs?: boolean
}

type DesktopBootLocation = Pick<Location, 'href'>

const TRANSIENT_STATES = new Set<DesktopUiState>([
  'initializing',
  'preparing_database',
  'migrating',
  'starting_services',
  'restarting',
  'shutting_down',
])

const COPY: Record<DesktopUiState, { heading: string; explanation: string }> = {
  initializing: {
    heading: '正在准备 Poly',
    explanation: '正在检查本地运行环境，请稍候。',
  },
  preparing_database: {
    heading: '正在准备本地数据库',
    explanation: '正在安全地打开本地数据，请稍候。',
  },
  migrating: {
    heading: '正在升级本地数据',
    explanation: '正在使您的数据与当前版本兼容，请不要退出 Poly。',
  },
  starting_services: {
    heading: '正在启动服务',
    explanation: '本地服务正在启动，准备就绪后会自动进入工作台。',
  },
  restarting: {
    heading: '服务中断，正在重启',
    explanation: '工作台已暂停操作，恢复连接后会自动继续。',
  },
  keychain_denied: {
    heading: '无法访问系统钥匙串',
    explanation: '请允许 Poly 访问钥匙串，然后重试。',
  },
  migration_failed: {
    heading: '本地数据升级失败',
    explanation: '您的数据已保留，请查看诊断日志或重试。',
  },
  runtime_unavailable: {
    heading: '本地服务无法启动',
    explanation: '本地运行时未能就绪，请重试或查看诊断日志。',
  },
  permission_denied: {
    heading: '没有足够的系统权限',
    explanation: '请检查 Poly 的系统权限，然后重试。',
  },
  resource_missing: {
    heading: '桌面运行资源缺失',
    explanation: '应用所需的本地文件不完整，请重新安装 Poly 或查看诊断日志。',
  },
  protocol_failed: {
    heading: '本地服务通信失败',
    explanation: '收到了无法验证的本地服务消息，请重试。',
  },
  shutting_down: {
    heading: '正在安全退出',
    explanation: '正在完成本地清理并保存运行状态，请稍候。',
  },
}

export function desktopBootPropsFromLocation(
  location: DesktopBootLocation,
): DesktopBootScreenProps | null {
  let url: URL
  try {
    url = new URL(location.href)
  } catch {
    return null
  }
  if (
    url.protocol !== 'tauri:'
    || url.host !== 'localhost'
    || url.username !== ''
    || url.password !== ''
  ) return null

  const parameters = url.searchParams
  const state = parameters.get('desktop-state')
  if (!state || !isDesktopUiState(state)) return null

  return {
    state,
    canRevealLogs: parameters.get('can-reveal-logs') === 'true',
  }
}

function isDesktopUiState(value: string): value is DesktopUiState {
  return (DESKTOP_UI_STATES as readonly string[]).includes(value)
}

export function DesktopBootScreen({ state, canRevealLogs = false }: DesktopBootScreenProps) {
  const copy = COPY[state]
  const transient = TRANSIENT_STATES.has(state)
  const [actionError, setActionError] = useState<string | null>(null)

  const runAction = async (action: keyof DesktopAdapter) => {
    const adapter = window.__POLY_DESKTOP__
    if (!adapter) {
      setActionError('无法连接桌面端，请重试。')
      return
    }

    setActionError(null)
    try {
      await adapter[action]()
    } catch {
      setActionError('操作未能完成，请重试或查看诊断日志。')
    }
  }

  return (
    <main className="desktop-boot-shell">
      <section className="desktop-boot-card" aria-labelledby="desktop-boot-heading">
        <div className="desktop-boot-mark" aria-hidden="true">P</div>
        <h1 id="desktop-boot-heading">{copy.heading}</h1>
        <p data-testid="desktop-explanation">{copy.explanation}</p>
        {transient ? (
          <div
            className="desktop-boot-progress"
            role="progressbar"
            aria-label={copy.heading}
            aria-valuetext="处理中"
          >
            <span />
          </div>
        ) : (
          <button type="button" className="desktop-boot-primary" onClick={() => void runAction('retry')}>
            重试
          </button>
        )}
        {canRevealLogs && (
          <button type="button" className="desktop-boot-secondary" onClick={() => void runAction('revealLogs')}>
            在 Finder 中显示诊断日志
          </button>
        )}
        {actionError && <p className="desktop-boot-action-error" role="alert">{actionError}</p>}
      </section>
    </main>
  )
}
