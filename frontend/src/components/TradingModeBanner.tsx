import { LockKeyhole, Power } from 'lucide-react'

import type { TradingMode } from '../types/system'


interface TradingModeBannerProps {
  mode: TradingMode
  openingEnabled: boolean
}


const modeLabels: Record<TradingMode, string> = {
  read_only: 'READ ONLY',
  shadow: 'SHADOW',
  limited_auto: 'LIMITED AUTO',
}


export function TradingModeBanner({ mode, openingEnabled }: TradingModeBannerProps) {
  const realOrdersEnabled = mode === 'limited_auto' && openingEnabled

  return (
    <div className="mode-band">
      <div className="mode-primary">
        {realOrdersEnabled ? <Power size={16} /> : <LockKeyhole size={16} />}
        <strong>{modeLabels[mode]}</strong>
        <span>{realOrdersEnabled ? '真实订单已启用' : '真实订单已禁用'}</span>
      </div>
      <div className={realOrdersEnabled ? 'switch-state enabled' : 'switch-state'}>
        <Power size={15} />
        <span>开仓总开关</span>
        <b>{realOrdersEnabled ? 'ON' : 'OFF'}</b>
      </div>
    </div>
  )
}
