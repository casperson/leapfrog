import { useEffect, useMemo, useState } from 'react'
import { RefreshCw, Wifi, WifiOff } from 'lucide-react'
import { api } from '../api/client'

interface LogEntry {
  id: number
  timestamp: string
  level: string
  source: string
  message: string
}

interface LogHistoryPayload {
  entries: LogEntry[]
  next_since_id?: number | null
}

const LEVEL_OPTIONS = ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'] as const
const MAX_CLIENT_ENTRIES = 500

function buildLogQuery(level: string, source: string): string {
  const params = new URLSearchParams()
  if (level) {
    params.set('level', level)
  }
  if (source.trim()) {
    params.set('source', source.trim())
  }
  const query = params.toString()
  return query ? `?${query}` : ''
}

export default function LogConsole() {
  const [entries, setEntries] = useState<LogEntry[]>([])
  const [level, setLevel] = useState('')
  const [source, setSource] = useState('')
  const [connected, setConnected] = useState(false)
  const [loading, setLoading] = useState(true)
  const [live, setLive] = useState(true)

  const query = useMemo(() => buildLogQuery(level, source), [level, source])

  useEffect(() => {
    // Reload bounded history whenever filters change so the panel starts with
    // a stable replay snapshot before live SSE entries append.
    let active = true
    setLoading(true)
    api.get<LogHistoryPayload>(`/api/logs/history${query ? `${query}&limit=200` : '?limit=200'}`)
      .then(payload => {
        if (!active) {
          return
        }
        setEntries(payload.entries)
      })
      .catch(() => {
        if (active) {
          setEntries([])
        }
      })
      .finally(() => {
        if (active) {
          setLoading(false)
        }
      })

    return () => {
      active = false
    }
  }, [query])

  useEffect(() => {
    // Open one filtered SSE stream at a time; closing and recreating it on
    // filter changes avoids mixing entries from different log queries.
    if (!live || typeof EventSource === 'undefined') {
      setConnected(false)
      return
    }

    const params = new URLSearchParams()
    params.set('replay', '0')
    if (level) {
      params.set('level', level)
    }
    if (source.trim()) {
      params.set('source', source.trim())
    }

    const stream = new EventSource(`/api/logs/stream?${params.toString()}`)
    const handleLogEvent = (event: MessageEvent<string>) => {
      try {
        const entry = JSON.parse(event.data) as LogEntry
        setEntries(current => [...current, entry].slice(-MAX_CLIENT_ENTRIES))
      } catch {
        // ignore malformed SSE payloads
      }
    }
    stream.onopen = () => {
      setConnected(true)
    }
    stream.addEventListener('log', handleLogEvent as EventListener)
    // Preserve compatibility with default-message SSE payloads if the backend
    // ever emits them, but prefer the explicit custom `log` event.
    stream.onmessage = handleLogEvent
    stream.onerror = () => {
      setConnected(false)
    }

    return () => {
      stream.removeEventListener('log', handleLogEvent as EventListener)
      stream.close()
      setConnected(false)
    }
  }, [level, live, source])

  return (
    <section className="rounded-xl border border-plex-border bg-plex-card p-4 space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-lg font-semibold text-gray-100">Live Logs</h2>
          <p className="text-sm text-gray-500">Recent bounded history plus SSE updates from the local scanner.</p>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setLive(current => !current)}
            className={`inline-flex items-center gap-1 rounded-lg border px-3 py-1.5 text-xs transition-colors ${
              live
                ? 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300'
                : 'border-plex-border text-gray-300 hover:border-gray-500 hover:text-white'
            }`}
          >
            {connected ? <Wifi size={13} /> : <WifiOff size={13} />}
            {live ? 'Live' : 'Paused'}
          </button>
          <button
            onClick={() => {
              setLoading(true)
              api.get<LogHistoryPayload>(`/api/logs/history${query ? `${query}&limit=200` : '?limit=200'}`)
                .then(payload => setEntries(payload.entries))
                .catch(() => {})
                .finally(() => setLoading(false))
            }}
            className="inline-flex items-center gap-1 rounded-lg border border-plex-border px-3 py-1.5 text-xs text-gray-300 transition-colors hover:border-plex-orange/50 hover:text-white"
          >
            <RefreshCw size={13} />
            Reload
          </button>
        </div>
      </div>

      <div className="flex flex-wrap gap-2">
        <select
          value={level}
          onChange={event => setLevel(event.target.value)}
          className="rounded-lg border border-plex-border bg-black/10 px-3 py-2 text-sm text-gray-200 focus:border-plex-orange/60 focus:outline-none"
        >
          <option value="">All levels</option>
          {LEVEL_OPTIONS.map(option => (
            <option key={option} value={option}>{option}</option>
          ))}
        </select>
        <input
          value={source}
          onChange={event => setSource(event.target.value)}
          placeholder="Filter by module/source"
          className="min-w-[220px] flex-1 rounded-lg border border-plex-border bg-black/10 px-3 py-2 text-sm text-gray-200 placeholder:text-gray-600 focus:border-plex-orange/60 focus:outline-none"
        />
      </div>

      <div className="rounded-lg border border-plex-border bg-black/30">
        <div className="flex items-center justify-between border-b border-plex-border px-3 py-2 text-xs text-gray-500">
          <span>{entries.length} line{entries.length !== 1 ? 's' : ''}</span>
          <span>{connected ? 'Connected' : live ? 'Connecting…' : 'Live stream paused'}</span>
        </div>
        <div className="max-h-[68vh] overflow-y-auto font-mono text-xs">
          {loading ? (
            <div className="px-3 py-4 text-gray-500">Loading logs...</div>
          ) : entries.length === 0 ? (
            <div className="px-3 py-4 text-gray-500">No logs match the current filters.</div>
          ) : (
            entries.map(entry => (
              <div key={entry.id} className="grid gap-2 border-b border-white/5 px-3 py-2 md:grid-cols-[170px,80px,220px,1fr]">
                <span className="text-gray-500">{new Date(entry.timestamp).toLocaleTimeString()}</span>
                <span className={`font-semibold ${
                  entry.level === 'ERROR' || entry.level === 'CRITICAL'
                    ? 'text-red-300'
                    : entry.level === 'WARNING'
                      ? 'text-amber-300'
                      : entry.level === 'DEBUG'
                        ? 'text-sky-300'
                        : 'text-emerald-300'
                }`}>
                  {entry.level}
                </span>
                <span className="truncate text-sky-300">{entry.source}</span>
                <span className="whitespace-pre-wrap break-words text-gray-200">{entry.message}</span>
              </div>
            ))
          )}
        </div>
      </div>
    </section>
  )
}
