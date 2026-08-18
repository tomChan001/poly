import { BookOpenCheck } from 'lucide-react'


export function MappingQueuePage() {
  return (
    <div className="page">
      <section className="page-heading">
        <div><h2>映射审核</h2><p>只有人工确认的 EXACT 映射可进入自动执行</p></div>
        <span className="count-badge">0 待审核</span>
      </section>
      <section className="empty-panel">
        <BookOpenCheck size={28} />
        <strong>审核队列为空</strong>
        <span>Oddpool 新候选完成原生规则解析后会出现在这里</span>
      </section>
    </div>
  )
}

