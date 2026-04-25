import { useEffect, useMemo, useState } from 'react'
import { ArrowBigDown, ArrowBigUp, ChevronsDown, ChevronsUp, RefreshCw, StopCircle, Trash2, Zap } from 'lucide-react'
import { api } from '../api/client'
import { usePageVisibility } from '../lib/polling'
import { formatTimestamp } from '../lib/scan'

interface QueueJob {
  plex_guid: string
  title: string
  queue_position?: number | null
  queue_priority?: number
  queue_reason?: string
  queued_at?: string | null
  queue_updated_at?: string | null
  cancel_requested?: boolean
  status?: string
}

interface ActiveScan {
  guid: string
  title: string
  status: string
  progress: number
  cancel_requested?: boolean
}

interface QueueSnapshot {
  jobs: QueueJob[]
  queue_size: number
  current: string | null
  currents: string[]
  active_scans: ActiveScan[]
  paused: boolean
}

function normalizeQueueSnapshot(payload: Partial<QueueSnapshot> | null | undefined): QueueSnapshot {
  return {
    jobs: Array.isArray(payload?.jobs) ? payload.jobs : [],
    queue_size: typeof payload?.queue_size === 'number' ? payload.queue_size : 0,
    current: payload?.current ?? null,
    currents: Array.isArray(payload?.currents) ? payload.currents : [],
    active_scans: Array.isArray(payload?.active_scans) ? payload.active_scans : [],
    paused: Boolean(payload?.paused),
  }
}

export default function QueueManager() {
  const isPageVisible = usePageVisibility()
  const [snapshot, setSnapshot] = useState<QueueSnapshot | null>(null)
  const [loading, setLoading] = useState(true)
  const [selected, setSelected] = useState<string[]>([])
  const [mutating, setMutating] = useState<string | null>(null)

  const refreshQueue = async (signal?: AbortSignal) => {
    const payload = await api.get<Partial<QueueSnapshot>>('/api/scan/queue', { signal })
    setSnapshot(normalizeQueueSnapshot(payload))
  }

  const queuePollMs = useMemo(() => {
    const hasQueuedWork = (snapshot?.jobs.length ?? 0) > 0 || (snapshot?.active_scans.length ?? 0) > 0
    if (!isPageVisible) {
      return hasQueuedWork ? 15_000 : 0
    }
    return hasQueuedWork ? 3_000 : 30_000
  }, [isPageVisible, snapshot])

  useEffect(() => {
    // Queue polling stays fast while work is active, then backs off heavily
    // when the queue is idle or the tab is hidden to avoid useless churn.
    let controller = new AbortController()
    let timeoutId: number | null = null
    let cancelled = false

    const tick = async () => {
      controller.abort()
      controller = new AbortController()
      try {
        await refreshQueue(controller.signal)
      } catch {
        // ignore abort/poll failures
      } finally {
        setLoading(false)
        if (!cancelled && queuePollMs > 0) {
          timeoutId = window.setTimeout(() => {
            void tick()
          }, queuePollMs)
        }
      }
    }

    void tick()
    return () => {
      cancelled = true
      if (timeoutId != null) {
        window.clearTimeout(timeoutId)
      }
      controller.abort()
    }
  }, [queuePollMs])

  useEffect(() => {
    // Keep selection bounded to currently-visible queued jobs after reorder,
    // cancel, or refresh responses change the snapshot.
    const valid = new Set((snapshot?.jobs ?? []).map(job => job.plex_guid))
    setSelected(prev => prev.filter(guid => valid.has(guid)))
  }, [snapshot])

  const allQueuedSelected = useMemo(() => {
    const jobs = snapshot?.jobs ?? []
    return jobs.length > 0 && jobs.every(job => selected.includes(job.plex_guid))
  }, [selected, snapshot])

  const runMutation = async (key: string, path: string, body?: unknown) => {
    try {
      setMutating(key)
      const response = await api.post<QueueSnapshot>(path, body)
      setSnapshot(normalizeQueueSnapshot(response))
    } catch (err: any) {
      alert(err?.message || 'Queue update failed.')
    } finally {
      setMutating(null)
    }
  }

  const toggleSelected = (guid: string) => {
    setSelected(current => (
      current.includes(guid)
        ? current.filter(item => item !== guid)
        : [...current, guid]
    ))
  }

  return (
    <section className="rounded-xl border border-plex-border bg-plex-card p-4 space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-lg font-semibold text-gray-100">Scan Queue</h2>
          <p className="text-sm text-gray-500">
            {snapshot?.queue_size ?? 0} queued title{snapshot && snapshot.queue_size !== 1 ? 's' : ''}
          </p>
        </div>
        <button
          onClick={() => void refreshQueue()}
          className="inline-flex items-center gap-1 rounded-lg border border-plex-border px-3 py-1.5 text-xs text-gray-300 transition-colors hover:border-plex-orange/50 hover:text-white"
        >
          <RefreshCw size={13} />
          Refresh
        </button>
      </div>

      <div className="flex flex-wrap items-center gap-2 rounded-lg border border-plex-border bg-black/10 px-3 py-2">
        <button
          onClick={() => void runMutation('queue-unscanned-movies', '/api/scan/queue-unscanned', { media_type: 'movie', now: false })}
          disabled={mutating === 'queue-unscanned-movies'}
          className="inline-flex items-center gap-1 rounded-lg border border-plex-border px-3 py-1.5 text-xs text-gray-300 transition-colors hover:border-plex-orange/50 hover:text-white disabled:opacity-40"
        >
          <RefreshCw size={13} />
          Queue unscanned movies
        </button>
        <button
          onClick={() => void runMutation('scan-unscanned-movies-now', '/api/scan/queue-unscanned', { media_type: 'movie', now: true })}
          disabled={mutating === 'scan-unscanned-movies-now'}
          className="inline-flex items-center gap-1 rounded-lg border border-plex-orange/40 bg-plex-orange/10 px-3 py-1.5 text-xs text-plex-orange transition-colors hover:bg-plex-orange/20 disabled:opacity-40"
        >
          <Zap size={13} />
          Scan unscanned movies now
        </button>
      </div>

      <div className="grid gap-4 xl:grid-cols-[1.1fr,1.4fr]">
        <div className="space-y-3">
          <div>
            <h3 className="text-sm font-semibold text-gray-100">Active</h3>
            <div className="mt-2 space-y-2">
              {loading ? (
                <div className="text-sm text-gray-500">Loading queue...</div>
              ) : (snapshot?.active_scans ?? []).length === 0 ? (
                <div className="rounded-lg border border-plex-border bg-black/10 px-3 py-4 text-sm text-gray-500">
                  No active scan.
                </div>
              ) : (
                snapshot?.active_scans.map(scan => (
                  <div key={scan.guid} className="rounded-lg border border-plex-orange/20 bg-plex-orange/5 px-3 py-3">
                    <div className="flex items-center justify-between gap-3">
                      <div className="min-w-0">
                        <p className="truncate text-sm font-medium text-gray-100">{scan.title}</p>
                        <p className="text-xs text-gray-500">{Math.round(scan.progress * 100)}% complete</p>
                      </div>
                      <button
                        onClick={() => void runMutation(`active:${scan.guid}`, `/api/scan/active/${encodeURIComponent(scan.guid)}/cancel`)}
                        disabled={mutating === `active:${scan.guid}`}
                        className="inline-flex items-center gap-1 rounded-lg border border-red-500/30 bg-red-500/10 px-2 py-1.5 text-xs text-red-300 transition-colors hover:bg-red-500/20 disabled:opacity-40"
                      >
                        <StopCircle size={13} />
                        Cancel
                      </button>
                    </div>
                    <div className="mt-2 h-1.5 overflow-hidden rounded-full bg-plex-border">
                      <div className="h-full rounded-full bg-plex-orange transition-all duration-700" style={{ width: `${scan.progress * 100}%` }} />
                    </div>
                  </div>
                ))
              )}
            </div>
          </div>
        </div>

        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2 rounded-lg border border-plex-border bg-black/10 px-3 py-2">
            <label className="inline-flex items-center gap-2 text-xs text-gray-300">
              <input
                type="checkbox"
                checked={allQueuedSelected}
                onChange={() => setSelected(allQueuedSelected ? [] : (snapshot?.jobs ?? []).map(job => job.plex_guid))}
                className="h-4 w-4 accent-plex-orange"
              />
              Select all queued
            </label>
            <button
              onClick={() => void runMutation('cancel-selected', '/api/scan/queue/cancel-selected', { guids: selected })}
              disabled={selected.length === 0 || mutating === 'cancel-selected'}
              className="inline-flex items-center gap-1 rounded-lg border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-xs text-amber-300 transition-colors hover:bg-amber-500/20 disabled:opacity-40"
            >
              <Trash2 size={13} />
              Cancel selected
            </button>
            <button
              onClick={() => void runMutation('cancel-all', '/api/scan/queue/cancel-all')}
              disabled={(snapshot?.jobs.length ?? 0) === 0 || mutating === 'cancel-all'}
              className="inline-flex items-center gap-1 rounded-lg border border-red-500/30 bg-red-500/10 px-2.5 py-1.5 text-xs text-red-300 transition-colors hover:bg-red-500/20 disabled:opacity-40"
            >
              <Trash2 size={13} />
              Cancel all queued
            </button>
          </div>

          {(snapshot?.jobs.length ?? 0) === 0 ? (
            <div className="rounded-lg border border-plex-border bg-black/10 px-3 py-4 text-sm text-gray-500">
              Queue is empty.
            </div>
          ) : (
            <div className="space-y-2">
              {snapshot?.jobs.map(job => (
                <div key={job.plex_guid} className="rounded-lg border border-plex-border bg-black/10 px-3 py-3">
                  <div className="flex items-start gap-3">
                    <input
                      type="checkbox"
                      checked={selected.includes(job.plex_guid)}
                      onChange={() => toggleSelected(job.plex_guid)}
                      className="mt-1 h-4 w-4 flex-shrink-0 accent-plex-orange"
                    />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        <span className="rounded-full border border-blue-500/20 bg-blue-500/10 px-2 py-1 text-[11px] uppercase tracking-wide text-blue-300">
                          #{job.queue_position ?? 'n/a'}
                        </span>
                        <p className="truncate text-sm font-medium text-gray-100">{job.title}</p>
                      </div>
                      <div className="mt-2 flex flex-wrap gap-3 text-xs text-gray-500">
                        {job.queued_at && <span>Queued {formatTimestamp(job.queued_at)}</span>}
                        {job.queue_updated_at && <span>Updated {formatTimestamp(job.queue_updated_at)}</span>}
                        {job.cancel_requested && <span className="text-amber-300">Cancel requested</span>}
                      </div>
                    </div>
                    <div className="flex flex-wrap items-center gap-1">
                      <button
                        title="Move to top"
                        aria-label={`Move ${job.title} to top`}
                        onClick={() => void runMutation(`top:${job.plex_guid}`, `/api/scan/queue/${encodeURIComponent(job.plex_guid)}/move-top`)}
                        disabled={mutating === `top:${job.plex_guid}`}
                        className="rounded-lg border border-plex-border p-2 text-gray-300 transition-colors hover:border-plex-orange/50 hover:text-white disabled:opacity-40"
                      >
                        <ChevronsUp size={13} />
                      </button>
                      <button
                        title="Move up"
                        aria-label={`Move ${job.title} up`}
                        onClick={() => void runMutation(`up:${job.plex_guid}`, `/api/scan/queue/${encodeURIComponent(job.plex_guid)}/move-up`)}
                        disabled={mutating === `up:${job.plex_guid}`}
                        className="rounded-lg border border-plex-border p-2 text-gray-300 transition-colors hover:border-plex-orange/50 hover:text-white disabled:opacity-40"
                      >
                        <ArrowBigUp size={13} />
                      </button>
                      <button
                        title="Move down"
                        aria-label={`Move ${job.title} down`}
                        onClick={() => void runMutation(`down:${job.plex_guid}`, `/api/scan/queue/${encodeURIComponent(job.plex_guid)}/move-down`)}
                        disabled={mutating === `down:${job.plex_guid}`}
                        className="rounded-lg border border-plex-border p-2 text-gray-300 transition-colors hover:border-plex-orange/50 hover:text-white disabled:opacity-40"
                      >
                        <ArrowBigDown size={13} />
                      </button>
                      <button
                        title="Move to bottom"
                        aria-label={`Move ${job.title} to bottom`}
                        onClick={() => void runMutation(`bottom:${job.plex_guid}`, `/api/scan/queue/${encodeURIComponent(job.plex_guid)}/move-bottom`)}
                        disabled={mutating === `bottom:${job.plex_guid}`}
                        className="rounded-lg border border-plex-border p-2 text-gray-300 transition-colors hover:border-plex-orange/50 hover:text-white disabled:opacity-40"
                      >
                        <ChevronsDown size={13} />
                      </button>
                      <button
                        title="Cancel queued item"
                        aria-label={`Cancel ${job.title}`}
                        onClick={() => void runMutation(`cancel:${job.plex_guid}`, `/api/scan/queue/${encodeURIComponent(job.plex_guid)}/cancel`)}
                        disabled={mutating === `cancel:${job.plex_guid}`}
                        className="rounded-lg border border-red-500/30 bg-red-500/10 p-2 text-red-300 transition-colors hover:bg-red-500/20 disabled:opacity-40"
                      >
                        <Trash2 size={13} />
                      </button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </section>
  )
}
