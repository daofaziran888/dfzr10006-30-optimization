import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/diagnose'
const columns = [
  "报告编号",
  "诊断对象",
  "健康分值",
  "异常类型",
  "根因分析",
  "修复建议",
  "诊断耗时",
  "诊断时间"
]
const actions = [
  "开始诊断",
  "处理异常",
  "归档报告"
]
const statuses = [
  "草稿",
  "诊断中",
  "已生成",
  "已处理"
]
const stats = [
  {
    "label": "诊断报告总数",
    "value": 0
  },
  {
    "label": "已生成报告",
    "value": 0
  },
  {
    "label": "已处理报告",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

type WindowDiagnosis = Record<string, string | number | null>

export default function DiagnoseView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  // 指标采集窗口诊断：与采集数据快照读取同一窗口序号。
  const [collectorNo, setCollectorNo] = useState('METR-0003')
  const [windowSeq, setWindowSeq] = useState(1)
  const [windowDiagnosis, setWindowDiagnosis] = useState<WindowDiagnosis | null>(null)
  const [diagnoseNotice, setDiagnoseNotice] = useState('')

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('诊断报告列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '服务健康诊断列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('服务健康诊断动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '服务健康诊断操作失败')
    }
  }

  async function diagnoseMetricWindow() {
    setWindowDiagnosis(null)
    setDiagnoseNotice('')
    try {
      const response = await request(
        `/api/diagnose/metric-windows/${encodeURIComponent(collectorNo.trim())}?window_seq=${windowSeq}`,
      )
      const payload = await response.json()
      if (!response.ok) {
        setDiagnoseNotice(String(payload.detail ?? '窗口诊断失败'))
        return
      }
      setWindowDiagnosis(payload as WindowDiagnosis)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '窗口诊断请求失败')
    }
  }

  return (
    <section className="page" data-module="diagnose">
      <header className="page-head">
        <div>
          <h2>服务健康诊断管理</h2>
          <p className="page-desc">维护服务健康的异常检测、根因分析与修复建议，为服务健康诊断提供可审计的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('诊断报告登记入口尚未接入审批流')}>
            登记诊断报告
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出服务健康诊断清单
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
              <td className="empty-state" colSpan={columns.length + 1}>暂无服务健康诊断数据，可先登记诊断报告</td>
            </tr>
          )}
        </tbody>
      </table>

      <div className="panel">
        <div className="panel-head">
          <h3>指标采集窗口健康诊断（采集快照与诊断读取同一窗口序号）</h3>
        </div>
        <div className="form-grid">
          <label className="filter-item">
            <span>采集器编号</span>
            <input value={collectorNo} onChange={(event) => setCollectorNo(event.target.value)} placeholder="例如 METR-0003" />
          </label>
          <label className="filter-item">
            <span>采集窗口序号</span>
            <input
              type="number"
              min={1}
              value={windowSeq}
              onChange={(event) => setWindowSeq(Number(event.target.value) || 1)}
            />
          </label>
          <button className="btn primary" type="button" onClick={() => void diagnoseMetricWindow()}>
            读取同窗口快照并诊断
          </button>
        </div>
        {diagnoseNotice && <p className="notice-text">{diagnoseNotice}</p>}
        {windowDiagnosis && (
          <div className="result-box">
            <h4 className="sub-title">窗口序号 {String(windowDiagnosis['窗口序号'])} 诊断结果（{String(windowDiagnosis['窗口键'])}）</h4>
            <div className="form-grid">
              {Object.entries(windowDiagnosis).map(([key, value]) => (
                <div key={key} className="result-item">
                  <span className="stat-label">{key}</span>
                  <strong>{String(value ?? '—')}</strong>
                </div>
              ))}
            </div>
          </div>
        )}
      </div>

      <footer className="page-foot">
        <span>共 {total} 条服务健康诊断记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
