import { useState } from 'react'
import { Save } from 'lucide-react'


export function RiskSettingsPage() {
  const [saved, setSaved] = useState(false)

  return (
    <div className="page">
      <section className="page-heading"><div><h2>风控配置</h2><p>修改后生成不可变策略版本</p></div></section>
      <form className="settings-form" onSubmit={(event) => { event.preventDefault(); setSaved(true) }}>
        <div className="settings-group">
          <h3>收益与时间</h3>
          <label><span>最低保守 ROI</span><div className="input-suffix"><input type="number" defaultValue="3" min="0" step="0.1" /><b>%</b></div></label>
          <label><span>最长预计结算</span><div className="input-suffix"><input type="number" defaultValue="30" min="1" /><b>天</b></div></label>
          <label><span>行情最大年龄</span><div className="input-suffix"><input type="number" defaultValue="2" min="0.1" step="0.1" /><b>秒</b></div></label>
        </div>
        <div className="settings-group">
          <h3>资本限额</h3>
          <label><span>单笔上限</span><div className="input-prefix"><b>$</b><input type="number" defaultValue="10" min="1" /></div></label>
          <label><span>单事件上限</span><div className="input-prefix"><b>$</b><input type="number" defaultValue="25" min="1" /></div></label>
          <label><span>总未结算资本</span><div className="input-prefix"><b>$</b><input type="number" defaultValue="100" min="1" /></div></label>
        </div>
        <div className="settings-group">
          <h3>未对冲保护</h3>
          <label><span>最大未对冲时长</span><div className="input-suffix"><input type="number" defaultValue="2" min="0.1" step="0.1" /><b>秒</b></div></label>
          <label><span>最大未对冲损失</span><div className="input-prefix"><b>$</b><input type="number" defaultValue="2" min="0.01" step="0.01" /></div></label>
          <label><span>单笔风险缓冲</span><div className="input-prefix"><b>$</b><input type="number" defaultValue="0.25" min="0" step="0.01" /></div></label>
        </div>
        <div className="form-actions">
          {saved && <span className="saved-state">已生成新策略版本</span>}
          <button type="submit" className="primary-button"><Save size={16} />保存策略</button>
        </div>
      </form>
    </div>
  )
}
