import { CircleAlert, CircleCheck, LoaderCircle } from 'lucide-react'

import type { RuntimeStatus } from '../types/runtime'

interface Props {
  status: RuntimeStatus | null
}

export function RuntimeStatusBand({ status }: Props) {
  if (status === null) {
    return <div className="runtime-band pending"><LoaderCircle size={15} />正在读取运行状态</div>
  }

  const active = status.ready && status.running && !status.last_error
  const reason = status.last_error
    ?? (status.missing_providers.length > 0
      ? `待配置：${status.missing_providers.join('、')}`
      : !status.running ? '后台轮询未运行' : null)

  return (
    <div className={active ? 'runtime-band active' : 'runtime-band blocked'}>
      <div className="runtime-state">
        {active ? <CircleCheck size={15} /> : <CircleAlert size={15} />}
        <strong>{active ? '行情评估运行中' : '运行未就绪'}</strong>
        {reason && <span>{reason}</span>}
      </div>
      <div className="runtime-meta">
        <span>已启动 {status.executions_started} 次执行</span>
        <span>{status.last_cycle_at ? `最近轮询 ${new Date(status.last_cycle_at).toLocaleString('zh-CN')}` : '尚未轮询'}</span>
      </div>
    </div>
  )
}
