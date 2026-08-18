import { LockKeyhole, Power } from 'lucide-react'


interface TradingModeBannerProps {
  mode: 'READ ONLY' | 'SHADOW' | 'LIMITED AUTO'
  openingEnabled: boolean
}


export function TradingModeBanner({ mode, openingEnabled }: TradingModeBannerProps) {
  return (
    <div className="mode-band">
      <div className="mode-primary">
        <LockKeyhole size={16} />
        <strong>{mode}</strong>
        <span>真实订单已禁用</span>
      </div>
      <div className={openingEnabled ? 'switch-state enabled' : 'switch-state'}>
        <Power size={15} />
        <span>开仓总开关</span>
        <b>{openingEnabled ? 'ON' : 'OFF'}</b>
      </div>
    </div>
  )
}

