import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/metric'
const columns = [
  "采集器编号",
  "监控对象",
  "指标名称",
  "采集间隔",
  "聚合方式",
  "保留天数",
  "最近采集",
  "采集器状态"
]
const actions = [
  "启动采集",
  "暂停采集",
  "关闭采集"
]
const statuses = [
  "草稿",
  "采集中",
  "已暂停",
  "已关闭"
]
const stats = [
  {
    "label": "指标采集器总数",
    "value": 0
  },
  {
    "label": "采集中采集器",
    "value": 0
  },
  {
    "label": "已暂停采集器",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

type Watermark = {
  采集器编号: string
  水位窗口序号: number
  下一窗口序号: number
  当前批次: string
  当前聚合公式: string
  当前聚合口径: string
  未完成窗口数: number
  未完成窗口: string[]
  批次总数: number
}

type WindowItem = {
  window_key: string
  seq: number
  completed: boolean
  samples: number[]
  sample_count: number
  batch_no: string
  formula: string
  formula_label: string
  snapshot: { value: number | null; captured_at: string } | null
  updated_at: string
}

type Batch = {
  batch_no: string
  formula: string
  formula_label: string
  switch_seq: number | null
  status: string
  opened_at: string
  closed_at: string | null
}

type Diagnosis = Record<string, string | number | null>

type ActionEnvelope = {
  ok: boolean
  message: string
  entry: Record<string, unknown> | null
}

async function postJson(path: string, body: Record<string, unknown>): Promise<ActionEnvelope> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  return (await response.json()) as ActionEnvelope
}

export default function MetricView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  // ---- 采集窗口面板状态 ----
  const [selectedCollector, setSelectedCollector] = useState('')
  const [watermark, setWatermark] = useState<Watermark | null>(null)
  const [windows, setWindows] = useState<WindowItem[]>([])
  const [batches, setBatches] = useState<Batch[]>([])
  const [formulas, setFormulas] = useState<Record<string, string>>({})
  const [windowKeyInput, setWindowKeyInput] = useState('')
  const [samplesInput, setSamplesInput] = useState('')
  const [formulaInput, setFormulaInput] = useState('')
  const [notice, setNotice] = useState('')
  const [diagnosis, setDiagnosis] = useState<Diagnosis | null>(null)
  const [busy, setBusy] = useState(false)

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('指标采集器列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '服务指标采集列表读取失败')
    }
  }, [filters])

  const loadWindowPanel = useCallback(async (collectorNo: string) => {
    if (!collectorNo) {
      setWatermark(null)
      setWindows([])
      setBatches([])
      return
    }
    try {
      const [wmRes, winRes, batchRes] = await Promise.all([
        request(`${ENDPOINT}/collectors/${encodeURIComponent(collectorNo)}/watermark`),
        request(`${ENDPOINT}/collectors/${encodeURIComponent(collectorNo)}/windows`),
        request(`${ENDPOINT}/collectors/${encodeURIComponent(collectorNo)}/batches`),
      ])
      if (!wmRes.ok || !winRes.ok || !batchRes.ok) {
        throw new Error('采集窗口水位读取失败')
      }
      const wm = (await wmRes.json()) as Watermark
      const win = (await winRes.json()) as { 窗口列表: WindowItem[] }
      const batch = (await batchRes.json()) as { 批次历史: Batch[] }
      setWatermark(wm)
      setWindows(win.窗口列表 ?? [])
      setBatches(batch.批次历史 ?? [])
      setFormulaInput(wm.当前聚合公式)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '采集窗口数据读取失败')
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    void request(`${ENDPOINT}/formulas`).then(async (res) => {
      if (res.ok) {
        const payload = (await res.json()) as { formulas: Record<string, string> }
        setFormulas(payload.formulas ?? {})
      }
    })
  }, [])

  // 默认选中第一个采集器（METR-0001 / METR-0003 为修复样本）。
  useEffect(() => {
    if (!selectedCollector && rows.length > 0) {
      const first = String(rows[0]['采集器编号'] ?? '')
      if (first) {
        setSelectedCollector(first)
      }
    }
  }, [rows, selectedCollector])

  useEffect(() => {
    setDiagnosis(null)
    setNotice('')
    void loadWindowPanel(selectedCollector)
  }, [selectedCollector, loadWindowPanel])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('服务指标采集动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '服务指标采集操作失败')
    }
  }

  function parseSamples(): number[] {
    return samplesInput
      .split(/[,，\s]+/)
      .map((item) => item.trim())
      .filter(Boolean)
      .map((item) => Number(item))
      .filter((item) => Number.isFinite(item))
  }

  async function submitWindow(complete: boolean) {
    if (!selectedCollector) {
      return
    }
    setBusy(true)
    setNotice('')
    try {
      const result = await postJson(
        `${ENDPOINT}/collectors/${encodeURIComponent(selectedCollector)}/windows/ingest`,
        {
          values: {
            window_key: windowKeyInput.trim() || undefined,
            samples: parseSamples(),
            complete,
          },
        },
      )
      setNotice(result.message)
      if (result.ok) {
        setSamplesInput('')
        setWindowKeyInput('')
      }
      await loadWindowPanel(selectedCollector)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '采集窗口提交失败')
    } finally {
      setBusy(false)
    }
  }

  async function recoverWindows() {
    if (!selectedCollector) {
      return
    }
    setBusy(true)
    try {
      const result = await postJson(
        `${ENDPOINT}/collectors/${encodeURIComponent(selectedCollector)}/windows/recover`,
        {},
      )
      setNotice(result.message)
      await loadWindowPanel(selectedCollector)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '故障恢复失败')
    } finally {
      setBusy(false)
    }
  }

  async function switchFormula() {
    if (!selectedCollector || !formulaInput) {
      return
    }
    setBusy(true)
    try {
      const result = await postJson(
        `${ENDPOINT}/collectors/${encodeURIComponent(selectedCollector)}/formula/switch`,
        { values: { formula: formulaInput } },
      )
      setNotice(result.message)
      await loadWindowPanel(selectedCollector)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '聚合公式切换失败')
    } finally {
      setBusy(false)
    }
  }

  async function diagnoseWindow(seq: number) {
    setDiagnosis(null)
    try {
      const res = await request(
        `/api/diagnose/metric-windows/${encodeURIComponent(selectedCollector)}?window_seq=${seq}`,
      )
      const payload = await res.json()
      if (!res.ok) {
        setNotice(String(payload.detail ?? '健康诊断失败'))
        return
      }
      setDiagnosis(payload as Diagnosis)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '健康诊断请求失败')
    }
  }

  const watermarkCards = watermark
    ? [
        { label: '持久水位（窗口序号）', value: watermark.水位窗口序号 },
        { label: '下一窗口序号', value: watermark.下一窗口序号 },
        { label: '未完成窗口（断点）', value: watermark.未完成窗口数 },
        { label: '当前聚合口径', value: `${watermark.当前聚合口径}（${watermark.当前批次}）` },
      ]
    : []

  return (
    <section className="page" data-module="metric">
      <header className="page-head">
        <div>
          <h2>服务指标采集管理</h2>
          <p className="page-desc">维护服务指标的采集间隔、聚合方式与保留策略，为服务指标采集提供可审计的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('指标采集器登记入口尚未接入审批流')}>
            登记指标采集器
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出服务指标采集清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无服务指标采集数据，可先登记指标采集器</td>
            </tr>
          )}
        </tbody>
      </table>

      <div className="panel">
        <div className="panel-head">
          <h3>采集窗口管理（完成标记 · 断点续采 · 持久水位 · 公式批次）</h3>
          <label className="filter-item">
            <span>选择采集器</span>
            <select value={selectedCollector} onChange={(event) => setSelectedCollector(event.target.value)}>
              {rows.map((row) => {
                const no = String(row['采集器编号'] ?? '')
                return <option key={no} value={no}>{no}</option>
              })}
            </select>
          </label>
        </div>

        {watermark && (
          <>
            <div className="stat-row">
              {watermarkCards.map((card) => (
                <article key={card.label} className="stat-card">
                  <span className="stat-label">{card.label}</span>
                  <strong className="stat-value">{card.value}</strong>
                </article>
              ))}
            </div>

            <div className="form-grid">
              <label className="filter-item">
                <span>窗口键（留空自动按水位分配）</span>
                <input
                  value={windowKeyInput}
                  placeholder="例如 win-0002，留空自动分配"
                  onChange={(event) => setWindowKeyInput(event.target.value)}
                />
              </label>
              <label className="filter-item">
                <span>本批样本（逗号分隔，失败部分进断点）</span>
                <input
                  value={samplesInput}
                  placeholder="例如 12, 15, 9"
                  onChange={(event) => setSamplesInput(event.target.value)}
                />
              </label>
            </div>
            <div className="row-actions" style={{ margin: '8px 0' }}>
              <button className="btn" type="button" disabled={busy} onClick={() => void submitWindow(false)}>
                写入断点（窗口未完成）
              </button>
              <button className="btn primary" type="button" disabled={busy} onClick={() => void submitWindow(true)}>
                完成窗口（打完成标记）
              </button>
              <button className="btn" type="button" disabled={busy} onClick={() => void recoverWindows()}>
                故障恢复（仅处理未完成窗口）
              </button>
              <button className="btn ghost" type="button" onClick={() => void loadWindowPanel(selectedCollector)}>
                刷新水位
              </button>
            </div>

            <div className="form-grid">
              <label className="filter-item">
                <span>聚合公式切换（旧快照保留旧口径，新窗口生效）</span>
                <select value={formulaInput} onChange={(event) => setFormulaInput(event.target.value)}>
                  {Object.entries(formulas).map(([code, label]) => (
                    <option key={code} value={code}>{label}（{code}）</option>
                  ))}
                </select>
              </label>
              <button className="btn" type="button" disabled={busy} onClick={() => void switchFormula()}>
                切换聚合公式并归档旧批次
              </button>
            </div>

            {notice && <p className="notice-text">{notice}</p>}

            <h4 className="sub-title">窗口关联清单（与持久水位同源，重新打开页面显示相同水位）</h4>
            <table className="data-table">
              <thead>
                <tr>
                  <th>窗口键</th>
                  <th>窗口序号</th>
                  <th>完成标记</th>
                  <th>样本数</th>
                  <th>断点/样本</th>
                  <th>聚合口径</th>
                  <th>公式批次</th>
                  <th>快照聚合值</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {windows.map((item) => (
                  <tr key={item.window_key}>
                    <td>{item.window_key}</td>
                    <td>{item.seq}</td>
                    <td>
                      {item.completed
                        ? <span className="tag tag-ok">已完成</span>
                        : <span className="tag tag-pending">未完成（断点）</span>}
                    </td>
                    <td>{item.sample_count}</td>
                    <td>{item.samples.join(', ') || '—'}</td>
                    <td>{item.formula_label}</td>
                    <td>{item.batch_no}</td>
                    <td>{item.snapshot ? String(item.snapshot.value) : '—'}</td>
                    <td>
                      <button className="link" type="button" onClick={() => void diagnoseWindow(item.seq)}>
                        健康诊断
                      </button>
                    </td>
                  </tr>
                ))}
                {windows.length === 0 && (
                  <tr>
                    <td className="empty-state" colSpan={9}>该采集器暂无采集窗口，提交第一批样本后建立</td>
                  </tr>
                )}
              </tbody>
            </table>

            <h4 className="sub-title">公式批次历史（旧口径归档批次）</h4>
            <table className="data-table">
              <thead>
                <tr>
                  <th>批次编号</th>
                  <th>聚合口径</th>
                  <th>切换起始窗口序号</th>
                  <th>批次状态</th>
                  <th>启用时间</th>
                  <th>归档时间</th>
                </tr>
              </thead>
              <tbody>
                {batches.map((batch) => (
                  <tr key={batch.batch_no}>
                    <td>{batch.batch_no}</td>
                    <td>{batch.formula_label}（{batch.formula}）</td>
                    <td>{batch.switch_seq ?? '首批次'}</td>
                    <td>
                      {batch.status === '生效中'
                        ? <span className="tag tag-ok">{batch.status}</span>
                        : <span className="tag tag-muted">{batch.status}</span>}
                    </td>
                    <td>{batch.opened_at}</td>
                    <td>{batch.closed_at ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>

            {diagnosis && (
              <div className="result-box">
                <h4 className="sub-title">服务健康诊断结果（读取同一窗口序号 {String(diagnosis['窗口序号'])} 的采集快照）</h4>
                <div className="form-grid">
                  {Object.entries(diagnosis).map(([key, value]) => (
                    <div key={key} className="result-item">
                      <span className="stat-label">{key}</span>
                      <strong>{String(value ?? '—')}</strong>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </>
        )}
      </div>

      <footer className="page-foot">
        <span>共 {total} 条服务指标采集记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
