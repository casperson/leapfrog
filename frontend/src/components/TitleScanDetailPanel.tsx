import { useEffect, useState } from 'react'
import { RefreshCw, X } from 'lucide-react'
import { CategoryDefinition, getCategoryLabel } from '../lib/categories'
import {
  analysisStateClassName,
  formatAnalysisState,
  formatStageKey,
  formatTimestamp,
  scanStatusClassName,
} from '../lib/scan'
import { api } from '../api/client'

interface ScanStatusRow {
  category: string
  status: string
  source: string
  detail: string
  segment_count: number
  progress: number
  updated_at?: string
}

interface StageStatusRow {
  stage_key: string
  category?: string | null
  status: string
  source: string
  detail: string
  progress: number
  created_at?: string
  updated_at?: string
}

interface ScanDetailPayload {
  media_id: string
  title: string
  analysis_state: string
  segment_count: number
  segment_counts_by_category: Record<string, number>
  scan_statuses: ScanStatusRow[]
  stage_statuses: StageStatusRow[]
  last_scan_time?: string | null
  preference_resolution_success?: boolean | null
  effective_segment_count?: number | null
  queue: {
    state: string
    position?: number | null
    priority?: number
    cancel_requested?: boolean
  }
  job_status: string
}

interface TitleScanDetailPanelProps {
  plexGuid: string
  title: string
  categories: CategoryDefinition[]
  reviewUser?: string | null
  onClose?: () => void
  compact?: boolean
}

function QueueSummary({ detail }: { detail: ScanDetailPayload }) {
  if (detail.queue.state !== 'queued' && detail.job_status !== 'scanning') {
    return null
  }

  return (
    <div className="flex flex-wrap gap-2 text-xs text-gray-300">
      {detail.queue.state === 'queued' && (
        <span className="rounded-full border border-blue-500/20 bg-blue-500/10 px-2 py-1 text-blue-300">
          Queue position {detail.queue.position ?? 'n/a'}
        </span>
      )}
      {detail.job_status === 'scanning' && (
        <span className="rounded-full border border-plex-orange/20 bg-plex-orange/10 px-2 py-1 text-plex-orange">
          Active scan
        </span>
      )}
      {detail.queue.cancel_requested && (
        <span className="rounded-full border border-amber-500/20 bg-amber-500/10 px-2 py-1 text-amber-300">
          Cancel requested
        </span>
      )}
    </div>
  )
}

export default function TitleScanDetailPanel({
  plexGuid,
  title,
  categories,
  reviewUser,
  onClose,
  compact = false,
}: TitleScanDetailPanelProps) {
  const [detail, setDetail] = useState<ScanDetailPayload | null>(null)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [error, setError] = useState('')

  const loadDetail = async (isRefresh = false) => {
    try {
      setError('')
      if (isRefresh) {
        setRefreshing(true)
      } else {
        setLoading(true)
      }
      const query = reviewUser ? `?user=${encodeURIComponent(reviewUser)}` : ''
      const payload = await api.get<ScanDetailPayload>(`/api/titles/${encodeURIComponent(plexGuid)}/scan-details${query}`)
      setDetail(payload)
    } catch (err: any) {
      setError(err?.message || 'Could not load scan details.')
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }

  useEffect(() => {
    // Reload detail whenever the selected title or review profile changes so
    // the panel always reflects the current effective skip view.
    void loadDetail()
  }, [plexGuid, reviewUser])

  return (
    <div className={`rounded-xl border border-plex-border bg-plex-card ${compact ? 'p-4' : 'p-5'} space-y-4`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-xs uppercase tracking-[0.2em] text-gray-500">Scan Detail</p>
          <h3 className="text-lg font-semibold text-gray-100 truncate">{title}</h3>
          {detail && (
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <span className={`rounded-full px-2 py-1 text-xs ${analysisStateClassName(detail.analysis_state)}`}>
                {formatAnalysisState(detail.analysis_state)}
              </span>
              <span className="rounded-full border border-plex-border bg-black/10 px-2 py-1 text-xs text-gray-300">
                {detail.segment_count} saved segment{detail.segment_count !== 1 ? 's' : ''}
              </span>
              {detail.last_scan_time && (
                <span className="text-xs text-gray-500">Updated {formatTimestamp(detail.last_scan_time)}</span>
              )}
            </div>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => void loadDetail(true)}
            className="inline-flex items-center gap-1 rounded-lg border border-plex-border px-3 py-1.5 text-xs text-gray-300 transition-colors hover:border-plex-orange/50 hover:text-white"
            disabled={refreshing}
          >
            <RefreshCw size={13} className={refreshing ? 'animate-spin' : ''} />
            Refresh
          </button>
          {onClose && (
            <button
              onClick={onClose}
              className="inline-flex items-center gap-1 rounded-lg border border-plex-border px-3 py-1.5 text-xs text-gray-300 transition-colors hover:border-gray-500 hover:text-white"
            >
              <X size={13} />
              Close
            </button>
          )}
        </div>
      </div>

      {loading ? (
        <div className="text-sm text-gray-500">Loading scan details...</div>
      ) : error ? (
        <div className="rounded-lg border border-red-500/20 bg-red-500/10 px-3 py-2 text-sm text-red-300">{error}</div>
      ) : detail ? (
        <>
          <QueueSummary detail={detail} />

          {reviewUser && detail.preference_resolution_success && detail.effective_segment_count != null && (
            <div className="rounded-lg border border-emerald-500/20 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-200">
              {detail.effective_segment_count} segment{detail.effective_segment_count !== 1 ? 's' : ''} would currently skip for {reviewUser}.
            </div>
          )}

          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
            {categories.map(category => {
              const row = detail.scan_statuses.find(entry => entry.category === category.key)
              const count = detail.segment_counts_by_category[category.key] ?? 0
              return (
                <div key={category.key} className="rounded-lg border border-plex-border bg-black/10 p-3">
                  <div className="flex items-start justify-between gap-2">
                    <div>
                      <p className="text-sm font-medium text-gray-100">{category.label}</p>
                      <p className="mt-1 text-xs text-gray-500">{count} segment{count !== 1 ? 's' : ''}</p>
                    </div>
                    <span className={`rounded-full border px-2 py-1 text-[11px] uppercase tracking-wide ${scanStatusClassName(row?.status)}`}>
                      {row?.status ?? 'pending'}
                    </span>
                  </div>
                  {row?.source && (
                    <p className="mt-2 text-xs text-sky-300">{row.source}</p>
                  )}
                  {row?.detail && (
                    <p className="mt-1 text-xs text-gray-400">{row.detail}</p>
                  )}
                  {row?.updated_at && (
                    <p className="mt-2 text-[11px] text-gray-500">{formatTimestamp(row.updated_at)}</p>
                  )}
                </div>
              )
            })}
          </div>

          <div className="space-y-2">
            <div className="flex items-center justify-between gap-2">
              <h4 className="text-sm font-semibold text-gray-100">Detector Timeline</h4>
              <span className="text-xs text-gray-500">{detail.stage_statuses.length} stage{detail.stage_statuses.length !== 1 ? 's' : ''}</span>
            </div>
            {detail.stage_statuses.length === 0 ? (
              <div className="rounded-lg border border-plex-border bg-black/10 px-3 py-4 text-sm text-gray-500">
                No stage activity recorded yet.
              </div>
            ) : (
              <div className="space-y-2">
                {detail.stage_statuses.map(stage => (
                  <div key={stage.stage_key} className="rounded-lg border border-plex-border bg-black/10 px-3 py-3">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-sm font-medium text-gray-100">{formatStageKey(stage.stage_key)}</span>
                      <span className={`rounded-full border px-2 py-1 text-[11px] uppercase tracking-wide ${scanStatusClassName(stage.status)}`}>
                        {stage.status}
                      </span>
                      {stage.category && (
                        <span className="rounded-full border border-plex-border bg-white/5 px-2 py-1 text-[11px] uppercase tracking-wide text-gray-300">
                          {getCategoryLabel(stage.category, categories)}
                        </span>
                      )}
                      {stage.source && (
                        <span className="rounded-full border border-sky-500/20 bg-sky-500/10 px-2 py-1 text-[11px] uppercase tracking-wide text-sky-300">
                          {stage.source}
                        </span>
                      )}
                    </div>
                    {stage.detail && (
                      <p className="mt-2 text-sm text-gray-300">{stage.detail}</p>
                    )}
                    <div className="mt-2 flex flex-wrap gap-3 text-[11px] text-gray-500">
                      <span>Progress {Math.round((stage.progress ?? 0) * 100)}%</span>
                      {stage.created_at && <span>Created {formatTimestamp(stage.created_at)}</span>}
                      {stage.updated_at && <span>Updated {formatTimestamp(stage.updated_at)}</span>}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </>
      ) : null}
    </div>
  )
}
