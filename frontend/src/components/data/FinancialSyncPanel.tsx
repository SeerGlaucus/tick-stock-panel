import { Loader2, RefreshCw } from 'lucide-react'
import { useFinancialStatus, useFinancialSync } from '@/lib/useFinancials'
import { toast } from '@/components/Toast'

/**
 * 财务数据同步面板 (数据页 · 财务数据卡片设置)。
 *
 * 与财务分析页共用同一套同步接口 (POST /api/financials/sync/{table}),
 * 只同步财务表本身, 不触发行情/指标管道 —— 解决"只想拉财务数据、
 * 不想把全部指标重算一遍"的场景。换手率重算仍由下方 EnrichedRebuildPanel 负责。
 */
const TABLE_LABELS: Record<string, string> = {
  metrics: '核心指标',
  income: '利润表',
  balance_sheet: '资产负债表',
  cash_flow: '现金流量表',
  shares: '股本表',
}
const TABLE_ORDER = ['metrics', 'income', 'balance_sheet', 'cash_flow', 'shares'] as const

export function FinancialSyncPanel() {
  const { data: status } = useFinancialStatus()
  const syncMut = useFinancialSync()
  const syncing = (status?.syncing ?? false) || syncMut.isPending
  const available = status?.available ?? false

  const run = (table: string) => {
    if (syncing) return
    syncMut.mutate(table, {
      onSuccess: (r) => {
        const synced = r?.synced
        if (synced && !synced.started) {
          if (synced.reason === 'already running') {
            toast('财务数据正在同步中，请稍候', 'success')
          } else if (synced.reason === 'no FINANCIAL capability') {
            toast('财务数据能力未就绪，请检查 API Key 或刷新页面后重试', 'error')
          } else {
            toast(`同步未能开始${synced.reason ? `: ${synced.reason}` : ''}`, 'error')
          }
        }
      },
    })
  }

  if (!available) {
    return (
      <div className="px-4 pb-4 pt-3 border-t border-border/40 text-[10px] leading-relaxed text-muted">
        财务数据不可用：当前未启用提供财务数据的数据源。可在「设置 → 数据源」中把「财务」切换为
        扶摇等支持财务的源。
      </div>
    )
  }

  return (
    <div className="px-4 pb-4 pt-3 border-t border-border/40 space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-xs font-medium text-foreground">同步财务数据</div>
        <span className="text-[10px] text-muted">只同步财务表，不重算行情指标</span>
      </div>
      <div className="flex flex-wrap gap-1.5">
        <button
          type="button"
          onClick={() => run('all')}
          disabled={syncing}
          className="inline-flex items-center gap-1 rounded px-2.5 py-1 text-[10px] font-medium bg-accent/15 text-accent hover:bg-accent/25 disabled:opacity-40 disabled:pointer-events-none transition-colors"
        >
          {syncing ? <Loader2 className="h-3 w-3 animate-spin" /> : <RefreshCw className="h-3 w-3" />}
          全部同步
        </button>
        {TABLE_ORDER.map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => run(t)}
            disabled={syncing}
            className="rounded px-2.5 py-1 text-[10px] text-muted/80 bg-elevated/60 hover:bg-accent/15 hover:text-accent disabled:opacity-40 disabled:pointer-events-none transition-colors"
          >
            {TABLE_LABELS[t]}
          </button>
        ))}
      </div>
      {syncing && (
        <div className="flex items-center gap-1.5 text-[10px] text-accent">
          <Loader2 className="h-3 w-3 animate-spin" />
          正在从财务数据源拉取数据… 可随时离开本页，同步在后台继续
        </div>
      )}
      <div className="text-[10px] leading-relaxed text-muted">
        单表同步只拉取该表；全部同步依次拉取 5 张财务表（核心指标 / 利润表 / 资产负债表 /
        现金流量表 / 股本表）。换手率重算见下方，需数据源提供历史股本表。
      </div>
    </div>
  )
}
