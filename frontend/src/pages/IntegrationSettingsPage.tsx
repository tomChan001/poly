import { useEffect, useMemo, useState } from 'react'
import { Check, LoaderCircle, PlugZap, Save, Trash2, X } from 'lucide-react'

import {
  deleteIntegrationSecret,
  getIntegrations,
  saveIntegration,
  testIntegration,
} from '../api/client'
import type {
  IntegrationConfig,
  IntegrationEnvironment,
  IntegrationProvider,
} from '../types/integration'

interface FieldDefinition {
  name: string
  label: string
  type?: 'number' | 'text'
}

interface ProviderDefinition {
  provider: IntegrationProvider
  name: string
  endpointLabel: string
  defaultUrl: string
  fields: FieldDefinition[]
  secretFields: FieldDefinition[]
  defaultConfiguration: Record<string, string | number | boolean>
}

interface EditableConfig {
  provider: IntegrationProvider
  enabled: boolean
  environment: IntegrationEnvironment
  baseUrl: string
  configuration: Record<string, string | number | boolean>
  advancedFunderAddress: string
  secrets: Record<string, string>
  secretStatus: IntegrationConfig['secret_status']
  version: number | null
}

const providers: ProviderDefinition[] = [
  {
    provider: 'oddpool',
    name: 'Oddpool',
    endpointLabel: 'Oddpool API 地址',
    defaultUrl: '',
    fields: [],
    secretFields: [{ name: 'api_token', label: 'Oddpool API Token' }],
    defaultConfiguration: {},
  },
  {
    provider: 'kalshi',
    name: 'Kalshi',
    endpointLabel: 'Kalshi API 地址',
    defaultUrl: 'https://api.elections.kalshi.com',
    fields: [{ name: 'key_id', label: 'Key ID' }],
    secretFields: [{ name: 'private_key', label: 'RSA 私钥' }],
    defaultConfiguration: { key_id: '' },
  },
  {
    provider: 'polymarket',
    name: 'Polymarket',
    endpointLabel: 'Polymarket CLOB 地址',
    defaultUrl: 'https://clob.polymarket.com',
    fields: [
      { name: 'chain_id', label: 'Chain ID', type: 'number' },
      { name: 'api_key', label: 'API Key' },
    ],
    secretFields: [
      { name: 'private_key', label: '钱包私钥（仅写入）' },
      { name: 'api_secret', label: 'API Secret' },
      { name: 'passphrase', label: 'Passphrase' },
    ],
    defaultConfiguration: {
      account_type: 'magic_proxy',
      owner_address: '',
      proxy_address: '',
      funder_address: '',
      signature_type: 1,
      chain_id: 137,
      api_key: '',
    },
  },
]

function emptyConfig(definition: ProviderDefinition): EditableConfig {
  return {
    provider: definition.provider,
    enabled: false,
    environment: 'production',
    baseUrl: definition.defaultUrl,
    configuration: { ...definition.defaultConfiguration },
    advancedFunderAddress: '',
    secrets: {},
    secretStatus: {},
    version: null,
  }
}

function fromServer(definition: ProviderDefinition, value: IntegrationConfig): EditableConfig {
  return {
    provider: value.provider,
    enabled: value.enabled,
    environment: value.environment,
    baseUrl: value.base_url,
    configuration: { ...definition.defaultConfiguration, ...value.configuration },
    advancedFunderAddress: String(value.configuration.funder_address ?? ''),
    secrets: {},
    secretStatus: value.secret_status,
    version: value.version,
  }
}

export function IntegrationSettingsPage() {
  const initial = useMemo(
    () => Object.fromEntries(providers.map((item) => [item.provider, emptyConfig(item)])) as Record<IntegrationProvider, EditableConfig>,
    [],
  )
  const [configs, setConfigs] = useState(initial)
  const [loading, setLoading] = useState(true)
  const [pending, setPending] = useState<string | null>(null)
  const [messages, setMessages] = useState<Partial<Record<IntegrationProvider, { ok: boolean; text: string }>>>({})

  useEffect(() => {
    getIntegrations()
      .then((values) => {
        setConfigs((current) => {
          const next = { ...current }
          for (const value of values) {
            const definition = providers.find((item) => item.provider === value.provider)
            if (definition) next[value.provider] = fromServer(definition, value)
          }
          return next
        })
      })
      .catch((error: unknown) => {
        const text = error instanceof Error ? error.message : '读取集成配置失败'
        setMessages(Object.fromEntries(providers.map((item) => [item.provider, { ok: false, text }])))
      })
      .finally(() => setLoading(false))
  }, [])

  const update = (provider: IntegrationProvider, patch: Partial<EditableConfig>) => {
    setConfigs((current) => ({
      ...current,
      [provider]: { ...current[provider], ...patch },
    }))
  }

  const updateConfiguration = (
    provider: IntegrationProvider,
    name: string,
    value: string | number,
  ) => {
    update(provider, {
      configuration: { ...configs[provider].configuration, [name]: value },
    })
  }

  const save = async (provider: IntegrationProvider) => {
    const config = configs[provider]
    const configuration = Object.fromEntries(
      Object.entries(config.configuration).filter(([, value]) => value !== ''),
    )
    if (provider === 'polymarket') {
      delete configuration.owner_address
      delete configuration.proxy_address
      delete configuration.funder_address
      if (
        ['gnosis_safe', 'deposit_wallet'].includes(String(configuration.account_type ?? ''))
        && config.advancedFunderAddress
      ) {
        configuration.funder_address = config.advancedFunderAddress
      }
    }
    setPending(`save:${provider}`)
    try {
      const saved = await saveIntegration(provider, {
        enabled: config.enabled,
        environment: config.environment,
        base_url: config.baseUrl,
        configuration,
        secrets: Object.fromEntries(
          Object.entries(config.secrets).filter(([, value]) => value.length > 0),
        ),
      })
      const definition = providers.find((item) => item.provider === provider)!
      setConfigs((current) => ({ ...current, [provider]: fromServer(definition, saved) }))
      setMessages((current) => ({ ...current, [provider]: { ok: true, text: `已保存版本 ${saved.version}` } }))
    } catch (error) {
      setMessages((current) => ({
        ...current,
        [provider]: { ok: false, text: error instanceof Error ? error.message : '保存失败' },
      }))
    } finally {
      setPending(null)
    }
  }

  const test = async (provider: IntegrationProvider) => {
    setPending(`test:${provider}`)
    try {
      const result = await testIntegration(provider)
      setMessages((current) => ({ ...current, [provider]: { ok: result.ok, text: result.detail } }))
    } catch (error) {
      setMessages((current) => ({
        ...current,
        [provider]: { ok: false, text: error instanceof Error ? error.message : '连接失败' },
      }))
    } finally {
      setPending(null)
    }
  }

  const removeSecret = async (provider: IntegrationProvider, name: string) => {
    setPending(`delete:${provider}:${name}`)
    try {
      const status = await deleteIntegrationSecret(provider, name)
      update(provider, {
        secretStatus: { ...configs[provider].secretStatus, [name]: status },
      })
      setMessages((current) => ({ ...current, [provider]: { ok: true, text: '凭证已删除' } }))
    } catch (error) {
      setMessages((current) => ({
        ...current,
        [provider]: { ok: false, text: error instanceof Error ? error.message : '删除失败' },
      }))
    } finally {
      setPending(null)
    }
  }

  return (
    <div className="page integrations-page">
      <section className="page-heading">
        <div><h2>集成配置</h2><p>平台连接、账户标识与凭证状态</p></div>
      </section>

      <div className="integration-list" aria-busy={loading}>
        {providers.map((definition) => {
          const config = configs[definition.provider]
          const message = messages[definition.provider]
          return (
            <form
              className="integration-panel"
              key={definition.provider}
              onSubmit={(event) => { event.preventDefault(); void save(definition.provider) }}
            >
              <header className="integration-head">
                <div>
                  <h3>{definition.name}</h3>
                  <span className={config.enabled ? 'provider-state enabled' : 'provider-state'}>
                    {config.enabled ? '已启用' : '未启用'}
                  </span>
                </div>
                <label className="toggle-control">
                  <input
                    type="checkbox"
                    checked={config.enabled}
                    onChange={(event) => update(definition.provider, { enabled: event.target.checked })}
                  />
                  <span>启用</span>
                </label>
              </header>

              <div className="integration-fields">
                <label className="field-wide">
                  <span>{definition.endpointLabel}</span>
                  <input
                    aria-label={definition.endpointLabel}
                    type="url"
                    required
                    value={config.baseUrl}
                    onChange={(event) => update(definition.provider, { baseUrl: event.target.value })}
                  />
                </label>
                <label>
                  <span>环境</span>
                  <select
                    aria-label={`${definition.name} 环境`}
                    value={config.environment}
                    onChange={(event) => update(definition.provider, { environment: event.target.value as IntegrationEnvironment })}
                  >
                    <option value="sandbox">Sandbox</option>
                    <option value="production">Production</option>
                  </select>
                </label>
                {definition.provider === 'polymarket' && (
                  <>
                    <div className="field-wide">
                      <p>Google / Magic 登录不需要密码，也不会在这里收集密码。</p>
                      <a
                        href="https://help.polymarket.com/en/articles/13364258-how-do-i-export-my-key"
                        target="_blank"
                        rel="noreferrer"
                      >
                        官方导出私钥说明
                      </a>
                    </div>
                    <label>
                      <span>账户类型</span>
                      <select
                        aria-label="Polymarket 账户类型"
                        value={String(config.configuration.account_type ?? 'magic_proxy')}
                        onChange={(event) => {
                          const accountType = event.target.value
                          const signatureTypes: Record<string, number> = {
                            magic_proxy: 1,
                            gnosis_safe: 2,
                            deposit_wallet: 3,
                            eoa: 0,
                          }
                          const compatibleConfiguration = Object.fromEntries(
                            Object.entries(config.configuration).filter(
                              ([name]) => ![
                                'owner_address',
                                'proxy_address',
                                'funder_address',
                              ].includes(name),
                            ),
                          )
                          update(definition.provider, {
                            configuration: {
                              ...compatibleConfiguration,
                              account_type: accountType,
                              signature_type: signatureTypes[accountType],
                            },
                            advancedFunderAddress: '',
                          })
                        }}
                      >
                        <option value="magic_proxy">Google / Magic Proxy</option>
                        <option value="eoa">EOA 钱包</option>
                        <option value="gnosis_safe">Gnosis Safe</option>
                        <option value="deposit_wallet">Deposit Wallet</option>
                      </select>
                    </label>
                    <label>
                      <span>派生 Owner 地址</span>
                      <input
                        aria-label="派生 Owner 地址"
                        readOnly
                        value={String(config.configuration.owner_address ?? '')}
                      />
                    </label>
                    <label>
                      <span>派生 Proxy 地址</span>
                      <input
                        aria-label="派生 Proxy 地址"
                        readOnly
                        value={String(config.configuration.proxy_address ?? '')}
                      />
                    </label>
                    <label>
                      <span>派生 Funder 地址</span>
                      <input
                        aria-label="派生 Funder 地址"
                        readOnly
                        value={String(config.configuration.funder_address ?? '')}
                      />
                    </label>
                    <label>
                      <span>派生签名类型</span>
                      <input
                        aria-label="派生签名类型"
                        readOnly
                        value={String(config.configuration.signature_type ?? 1)}
                      />
                    </label>
                    {['gnosis_safe', 'deposit_wallet'].includes(
                      String(config.configuration.account_type ?? ''),
                    ) && (
                      <label>
                        <span>高级 Funder 地址</span>
                        <input
                          aria-label="高级 Funder 地址"
                          value={config.advancedFunderAddress}
                          onChange={(event) => update(definition.provider, {
                            advancedFunderAddress: event.target.value,
                          })}
                        />
                      </label>
                    )}
                  </>
                )}
                {definition.fields.map((field) => (
                  <label key={field.name}>
                    <span>{field.label}</span>
                    <input
                      type={field.type ?? 'text'}
                      value={String(config.configuration[field.name] ?? '')}
                      onChange={(event) => updateConfiguration(
                        definition.provider,
                        field.name,
                        field.type === 'number' ? Number(event.target.value) : event.target.value,
                      )}
                    />
                  </label>
                ))}
              </div>

              <div className="credential-fields">
                {definition.secretFields.map((field) => {
                  const secretStatus = config.secretStatus[field.name]
                  const deleteKey = `delete:${definition.provider}:${field.name}`
                  return (
                    <div className="credential-row" key={field.name}>
                      <label>
                        <span>{field.label}</span>
                        <input
                          aria-label={field.label}
                          type="password"
                          autoComplete="new-password"
                          value={config.secrets[field.name] ?? ''}
                          placeholder={secretStatus?.configured ? '输入新值以替换' : ''}
                          onChange={(event) => update(definition.provider, {
                            secrets: { ...config.secrets, [field.name]: event.target.value },
                          })}
                        />
                      </label>
                      <div className="credential-status">
                        {secretStatus?.configured
                          ? <><Check size={14} /><code>{secretStatus.fingerprint}</code></>
                          : <><X size={14} /><span>未配置</span></>}
                      </div>
                      <button
                        type="button"
                        className="icon-button danger-button"
                        aria-label={`${definition.name} 清除 ${field.name} 凭据`}
                        title={`清除 ${field.label}`}
                        disabled={!secretStatus?.configured || pending === deleteKey}
                        onClick={() => void removeSecret(definition.provider, field.name)}
                      >
                        {pending === deleteKey ? <LoaderCircle className="spin" size={16} /> : <Trash2 size={16} />}
                      </button>
                    </div>
                  )
                })}
              </div>

              <footer className="integration-actions">
                <div className={message?.ok ? 'operation-message ok' : 'operation-message'}>
                  {message?.text ?? (config.version ? `配置版本 ${config.version}` : '')}
                </div>
                <button
                  type="button"
                  className="secondary-button"
                  disabled={config.version === null || pending !== null}
                  onClick={() => void test(definition.provider)}
                >
                  {pending === `test:${definition.provider}` ? <LoaderCircle className="spin" size={16} /> : <PlugZap size={16} />}
                  {definition.provider === 'polymarket' ? '测试连接（不会下单）' : '测试连接'}
                </button>
                <button type="submit" className="primary-button" disabled={pending !== null}>
                  {pending === `save:${definition.provider}` ? <LoaderCircle className="spin" size={16} /> : <Save size={16} />}
                  保存
                </button>
              </footer>
            </form>
          )
        })}
      </div>
    </div>
  )
}
