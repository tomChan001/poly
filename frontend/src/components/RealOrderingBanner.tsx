import { LockKeyhole, Power } from 'lucide-react'

interface RealOrderingBannerProps {
  enabled: boolean
}

export function RealOrderingBanner({ enabled }: RealOrderingBannerProps) {
  return (
    <div className={enabled ? 'real-ordering-banner enabled' : 'real-ordering-banner'}>
      {enabled ? <Power size={16} /> : <LockKeyhole size={16} />}
      <div>
        <strong>{enabled ? '真实下单已开启' : '真实下单已关闭'}</strong>
        <span>{enabled ? '符合风控条件的机会可以提交订单' : '仅评估和展示机会，不提交新订单'}</span>
      </div>
    </div>
  )
}
