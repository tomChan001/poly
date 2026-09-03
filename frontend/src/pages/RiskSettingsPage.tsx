import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { Save } from 'lucide-react'

import { getRiskPolicy, saveRiskPolicy } from '../api/client'
import type { RiskPolicy, RiskPolicyUpdate } from '../types/risk'

interface RiskForm {
  minimumRoiPercent: string
  maximumSettlementDays: string
  maximumBookAgeSeconds: string
  perTradeLimit: string
  perEventLimit: string
  portfolioLimit: string
  explicitCost: string
  riskBuffer: string
  maximumUnhedgedSeconds: string
  maximumUnhedgedLoss: string
  maximumArrivalGapSeconds: string
}

function moveDecimal(value: string, places: number): string {
  const match = value.trim().match(/^(-?)(\d*)(?:\.(\d*))?$/)
  if (!match || (!match[2] && !match[3])) return value

  const [, sign, whole = '', fraction = ''] = match
  const digits = `${whole}${fraction}` || '0'
  const decimalIndex = whole.length + places
  let result: string

  if (decimalIndex <= 0) {
    result = `0.${'0'.repeat(-decimalIndex)}${digits}`
  } else if (decimalIndex >= digits.length) {
    result = `${digits}${'0'.repeat(decimalIndex - digits.length)}`
  } else {
    result = `${digits.slice(0, decimalIndex)}.${digits.slice(decimalIndex)}`
  }

  const [integer, decimal = ''] = result.split('.')
  const normalizedInteger = integer.replace(/^0+(?=\d)/, '') || '0'
  const normalizedDecimal = decimal.replace(/0+$/, '')
  const normalized = normalizedDecimal ? `${normalizedInteger}.${normalizedDecimal}` : normalizedInteger
  return normalized === '0' ? '0' : `${sign}${normalized}`
}

const toPercent = (ratio: string) => moveDecimal(ratio, 2)
const toRatio = (percent: string) => moveDecimal(percent, -2)

function toForm(policy: RiskPolicy): RiskForm {
  return {
    minimumRoiPercent: toPercent(policy.minimum_roi),
    maximumSettlementDays: String(policy.maximum_settlement_days),
    maximumBookAgeSeconds: policy.maximum_book_age_seconds,
    perTradeLimit: policy.per_trade_limit,
    perEventLimit: policy.per_event_limit,
    portfolioLimit: policy.portfolio_limit,
    explicitCost: policy.explicit_cost,
    riskBuffer: policy.risk_buffer,
    maximumUnhedgedSeconds: policy.maximum_unhedged_seconds,
    maximumUnhedgedLoss: policy.maximum_unhedged_loss,
    maximumArrivalGapSeconds: policy.maximum_arrival_gap_seconds,
  }
}

function toUpdate(form: RiskForm): RiskPolicyUpdate {
  return {
    minimum_roi: toRatio(form.minimumRoiPercent),
    maximum_settlement_days: Number(form.maximumSettlementDays),
    maximum_book_age_seconds: form.maximumBookAgeSeconds,
    per_trade_limit: form.perTradeLimit,
    per_event_limit: form.perEventLimit,
    portfolio_limit: form.portfolioLimit,
    explicit_cost: form.explicitCost,
    risk_buffer: form.riskBuffer,
    maximum_unhedged_seconds: form.maximumUnhedgedSeconds,
    maximum_unhedged_loss: form.maximumUnhedgedLoss,
    maximum_arrival_gap_seconds: form.maximumArrivalGapSeconds,
  }
}

interface ValidationRule {
  name: keyof RiskForm
  label: string
  positive?: boolean
  integer?: boolean
}

const validationRules: ValidationRule[] = [
  { name: 'minimumRoiPercent', label: '最低保守 ROI' },
  { name: 'maximumSettlementDays', label: '最长预计结算', positive: true, integer: true },
  { name: 'maximumBookAgeSeconds', label: '行情最大年龄', positive: true },
  { name: 'perTradeLimit', label: '单笔上限', positive: true },
  { name: 'perEventLimit', label: '单事件上限', positive: true },
  { name: 'portfolioLimit', label: '总未结算资本', positive: true },
  { name: 'explicitCost', label: '显式成本' },
  { name: 'riskBuffer', label: '单笔风险缓冲' },
  { name: 'maximumUnhedgedSeconds', label: '最大未对冲时长', positive: true },
  { name: 'maximumUnhedgedLoss', label: '最大未对冲损失' },
  { name: 'maximumArrivalGapSeconds', label: '最大行情到达间隔', positive: true },
]

const plainDecimalPattern = /^-?(?:\d+(?:\.\d*)?|\.\d+)$/

function validateForm(form: RiskForm): string | null {
  for (const rule of validationRules) {
    const raw = form[rule.name].trim()
    if (!raw) return `${rule.label} 不能为空`
    if (!plainDecimalPattern.test(raw)) return `${rule.label} 必须使用普通十进制表示`

    const value = Number(raw)
    if (!Number.isFinite(value)) return `${rule.label}必须是有限数字`
    if (rule.integer && !Number.isInteger(value)) return `${rule.label}必须是整数`
    if (rule.name === 'minimumRoiPercent' && (value < 0 || value > 100)) {
      return '最低保守 ROI 必须在 0% 到 100% 之间'
    }
    if (rule.positive && value <= 0) return `${rule.label}必须大于 0`
    if (!rule.positive && value < 0) return `${rule.label}不能小于 0`
  }
  return null
}

function formatCreatedAt(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    dateStyle: 'medium',
    timeStyle: 'medium',
    timeZone: 'Asia/Shanghai',
  }).format(date)
}

interface RiskInputProps {
  label: string
  name: keyof RiskForm
  value: string
  onChange: (name: keyof RiskForm, value: string) => void
  prefix?: string
  suffix?: string
  min?: string
  max?: string
  step?: string
}

function RiskInput({ label, name, value, onChange, prefix, suffix, min, max, step }: RiskInputProps) {
  return (
    <label>
      <span>{label}</span>
      <div className={prefix ? 'input-prefix' : 'input-suffix'}>
        {prefix && <b>{prefix}</b>}
        <input
          aria-label={label}
          type="number"
          value={value}
          min={min}
          max={max}
          step={step}
          aria-required="true"
          onChange={(event) => onChange(name, event.target.value)}
        />
        {suffix && <b>{suffix}</b>}
      </div>
    </label>
  )
}

export function RiskSettingsPage() {
  const [policy, setPolicy] = useState<RiskPolicy | null>(null)
  const [form, setForm] = useState<RiskForm | null>(null)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState(false)
  const loadControllerRef = useRef<AbortController | null>(null)

  const loadPolicy = useCallback(async () => {
    loadControllerRef.current?.abort()
    const controller = new AbortController()
    loadControllerRef.current = controller
    setLoading(true)
    setError(null)
    try {
      const loadedPolicy = await getRiskPolicy(controller.signal)
      if (controller.signal.aborted || loadControllerRef.current !== controller) return
      setPolicy(loadedPolicy)
      setForm(toForm(loadedPolicy))
    } catch (reason) {
      if (controller.signal.aborted || loadControllerRef.current !== controller) return
      setError(`加载失败：${reason instanceof Error ? reason.message : '未知错误'}`)
    } finally {
      if (loadControllerRef.current === controller) {
        loadControllerRef.current = null
        setLoading(false)
      }
    }
  }, [])

  useEffect(() => {
    void loadPolicy()
    return () => {
      loadControllerRef.current?.abort()
      loadControllerRef.current = null
    }
  }, [loadPolicy])

  const updateField = (name: keyof RiskForm, value: string) => {
    if (!form) return
    const nextForm = { ...form, [name]: value }
    setForm(nextForm)
    setError(validateForm(nextForm))
    setSaved(false)
  }

  const savePolicy = async () => {
    if (!form || saving) return

    const validationError = validateForm(form)
    if (validationError) {
      setError(validationError)
      setSaved(false)
      return
    }

    setSaving(true)
    setError(null)
    try {
      const savedPolicy = await saveRiskPolicy(toUpdate(form))
      setPolicy(savedPolicy)
      setForm(toForm(savedPolicy))
      setSaved(true)
    } catch (reason) {
      setError(`保存失败：${reason instanceof Error ? reason.message : '未知错误'}`)
      setSaved(false)
    } finally {
      setSaving(false)
    }
  }

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    void savePolicy()
  }

  const disabled = loading || saving || !form

  return (
    <div className="page">
      <section className="page-heading">
        <div>
          <h2>风控配置</h2>
          <p>修改后生成不可变策略版本</p>
          {policy && (
            <p className="policy-metadata">
              <span>策略版本 {policy.version}</span> · <span>创建于 {formatCreatedAt(policy.created_at)}</span>
            </p>
          )}
        </div>
      </section>
      <form className="settings-form" onSubmit={handleSubmit} aria-busy={loading || saving} noValidate>
        <fieldset disabled={disabled}>
          {loading && <p className="form-status" role="status">正在加载风控策略…</p>}
          {form && (
            <>
              <div className="settings-group">
                <h3>收益与时间</h3>
                <RiskInput label="最低保守 ROI" name="minimumRoiPercent" value={form.minimumRoiPercent} onChange={updateField} suffix="%" min="0" max="100" step="any" />
                <RiskInput label="最长预计结算" name="maximumSettlementDays" value={form.maximumSettlementDays} onChange={updateField} suffix="天" min="1" step="1" />
                <RiskInput label="行情最大年龄" name="maximumBookAgeSeconds" value={form.maximumBookAgeSeconds} onChange={updateField} suffix="秒" min="0" step="any" />
              </div>
              <div className="settings-group">
                <h3>资本限额</h3>
                <RiskInput label="单笔上限" name="perTradeLimit" value={form.perTradeLimit} onChange={updateField} prefix="$" min="0" step="any" />
                <RiskInput label="单事件上限" name="perEventLimit" value={form.perEventLimit} onChange={updateField} prefix="$" min="0" step="any" />
                <RiskInput label="总未结算资本" name="portfolioLimit" value={form.portfolioLimit} onChange={updateField} prefix="$" min="0" step="any" />
              </div>
              <div className="settings-group">
                <h3>未对冲保护</h3>
                <RiskInput label="最大未对冲时长" name="maximumUnhedgedSeconds" value={form.maximumUnhedgedSeconds} onChange={updateField} suffix="秒" min="0" step="any" />
                <RiskInput label="最大未对冲损失" name="maximumUnhedgedLoss" value={form.maximumUnhedgedLoss} onChange={updateField} prefix="$" min="0" step="any" />
                <RiskInput label="单笔风险缓冲" name="riskBuffer" value={form.riskBuffer} onChange={updateField} prefix="$" min="0" step="any" />
              </div>
              <div className="settings-group">
                <h3>成本与行情同步</h3>
                <RiskInput label="显式成本" name="explicitCost" value={form.explicitCost} onChange={updateField} prefix="$" min="0" step="any" />
                <RiskInput label="最大行情到达间隔" name="maximumArrivalGapSeconds" value={form.maximumArrivalGapSeconds} onChange={updateField} suffix="秒" min="0" step="any" />
              </div>
            </>
          )}
          <div className="form-actions">
            {error && <span className="form-error" role="alert">{error}</span>}
            {saved && <span className="saved-state">已生成新策略版本</span>}
            <button type="submit" className="primary-button"><Save size={16} />{saving ? '保存中…' : '保存策略'}</button>
          </div>
        </fieldset>
        {!form && error && (
          <div className="form-retry">
            <button type="button" className="secondary-button" onClick={() => void loadPolicy()} disabled={loading}>重试</button>
          </div>
        )}
      </form>
    </div>
  )
}
