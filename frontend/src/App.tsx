import { useEffect, useMemo, useState } from 'react'
import {
  Activity,
  BookOpenCheck,
  ChartNoAxesCombined,
  Cable,
  Gauge,
  History,
  RefreshCw,
  Settings2,
  ShieldCheck,
} from 'lucide-react'

import { getExecutions, getOpportunities, getRuntimeStatus, getSystemStatus } from './api/client'
import { RuntimeStatusBand } from './components/RuntimeStatusBand'
import { TradingModeBanner } from './components/TradingModeBanner'
import { AnalyticsPage } from './pages/AnalyticsPage'
import { HistoryPage } from './pages/HistoryPage'
import { MappingQueuePage } from './pages/MappingQueuePage'
import { IntegrationSettingsPage } from './pages/IntegrationSettingsPage'
import { OpportunitiesPage } from './pages/OpportunitiesPage'
import { RiskSettingsPage } from './pages/RiskSettingsPage'
import type { Execution } from './types/execution'
import type { Opportunity } from './types/opportunity'
import type { RuntimeStatus } from './types/runtime'
import type { SystemStatus } from './types/system'
import './App.css'

type View = 'opportunities' | 'mappings' | 'analytics' | 'history' | 'settings' | 'integrations'

const navigation = [
  { id: 'opportunities' as const, label: '机会', icon: Gauge },
  { id: 'mappings' as const, label: '审核', icon: BookOpenCheck },
  { id: 'analytics' as const, label: '运行', icon: ChartNoAxesCombined },
  { id: 'history' as const, label: '历史', icon: History },
  { id: 'settings' as const, label: '风控', icon: Settings2 },
  { id: 'integrations' as const, label: '集成', icon: Cable },
]

function App() {
  const [view, setView] = useState<View>('opportunities')
  const [opportunities, setOpportunities] = useState<Opportunity[]>([])
  const [executions, setExecutions] = useState<Execution[]>([])
  const [runtimeStatus, setRuntimeStatus] = useState<RuntimeStatus | null>(null)
  const [systemStatus, setSystemStatus] = useState<SystemStatus>({
    status: 'ok',
    trading_mode: 'limited_auto',
    opening_enabled: true,
    reason: 'configured default',
  })
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const refresh = async () => {
    setLoading(true)
    try {
      const [latestOpportunities, latestExecutions, latestSystemStatus, latestRuntimeStatus] = await Promise.all([
        getOpportunities(),
        getExecutions(),
        getSystemStatus(),
        getRuntimeStatus(),
      ])
      setOpportunities(latestOpportunities)
      setExecutions(latestExecutions)
      setSystemStatus(latestSystemStatus)
      setRuntimeStatus(latestRuntimeStatus)
      setError(null)
    } catch {
      setError('无法读取机会数据')
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    void refresh()
  }, [])

  const activeCount = useMemo(
    () => opportunities.filter((item) => item.rejection_reasons.length === 0).length,
    [opportunities],
  )

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-mark" aria-hidden="true">
          <Activity size={20} />
        </div>
        <div className="brand-copy">
          <strong>POLYHEDGE</strong>
          <span>跨市场控制台</span>
        </div>
        <nav aria-label="主导航">
          {navigation.map((item) => {
            const Icon = item.icon
            return (
              <button
                key={item.id}
                type="button"
                className={view === item.id ? 'nav-item active' : 'nav-item'}
                onClick={() => setView(item.id)}
              >
                <Icon size={18} />
                <span>{item.label}</span>
              </button>
            )
          })}
        </nav>
        <div className="sidebar-foot">
          <ShieldCheck size={16} />
          <span>原生数据校验</span>
        </div>
      </aside>

      <main className="workspace">
        <header className="topbar">
          <div>
            <h1>跨市场控制台</h1>
            <p>Kalshi × Polymarket</p>
          </div>
          <div className="topbar-actions">
            <span className="live-indicator"><i />行情流</span>
            <button
              type="button"
              className="icon-button"
              title="刷新机会"
              aria-label="刷新机会"
              onClick={() => void refresh()}
            >
              <RefreshCw size={18} className={loading ? 'spin' : ''} />
            </button>
          </div>
        </header>

        <TradingModeBanner
          mode={systemStatus.trading_mode}
          openingEnabled={systemStatus.opening_enabled}
        />
        <RuntimeStatusBand status={runtimeStatus} />

        {error && <div className="error-band">{error}</div>}
        {view === 'opportunities' && (
          <OpportunitiesPage
            opportunities={opportunities}
            activeCount={activeCount}
            loading={loading}
          />
        )}
        {view === 'mappings' && <MappingQueuePage />}
        {view === 'analytics' && (
          <AnalyticsPage opportunities={opportunities} executions={executions} />
        )}
        {view === 'history' && <HistoryPage executions={executions} />}
        {view === 'settings' && <RiskSettingsPage />}
        {view === 'integrations' && <IntegrationSettingsPage />}
      </main>
    </div>
  )
}

export default App
