import { useEffect, useState } from 'react'
import { CheckSquare2, Download, Loader2, Search, Square, FileDown, XCircle } from 'lucide-react'
import { api } from '../api/client'

interface VidAngelTitle {
  media_id: string
  media_type: string
  title: string
  slug: string
  event_count: number
  category_count: number
  year?: number | null
  rating?: string | null
  season_number?: number | null
  episode_number?: number | null
  show_title?: string | null
}

interface VidAngelLeafFilter {
  leaf_key: string
  label: string
  description?: string | null
  category?: string | null
  category_label: string
  event_count: number
  tag_type?: string | null
  events: VidAngelEvent[]
}

interface VidAngelEvent {
  event_id: string
  start_ms: number
  end_ms: number
  description: string
  display_title?: string | null
  tag_type?: string | null
}

interface VidAngelCategory {
  key: string
  label: string
  description: string
  filters: VidAngelLeafFilter[]
}

interface VidAngelFilterCatalog {
  title: VidAngelTitle
  leaf_count: number
  event_count: number
  categories: VidAngelCategory[]
}

interface VidAngelCatalogResponse {
  available: boolean
  title_count: number
  titles: VidAngelTitle[]
}

function sanitizeFilename(value: string): string {
  return value
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, '-')
    .replace(/^-+|-+$/g, '') || 'untitled'
}

function formatVidAngelError(error: unknown, fallback: string): string {
  const message = error instanceof Error ? error.message : ''
  if (message.startsWith('503')) {
    return 'VidAngel export data is not ready yet. Run the VidAngel export first, then refresh this page.'
  }
  return message || fallback
}

function formatEventTime(valueMs: number): string {
  const totalSeconds = Math.max(0, valueMs) / 1000
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds - hours * 3600 - minutes * 60
  const fractionalText = (seconds - Math.floor(seconds))
    .toFixed(3)
    .slice(1)
    .replace(/0+$/, '')
    .replace(/\.$/, '')
  const secondsText = `${String(Math.floor(seconds)).padStart(2, '0')}${fractionalText}`
  return `${hours}:${String(minutes).padStart(2, '0')}:${secondsText}`
}

function eventIdsForFilter(filter: VidAngelLeafFilter): string[] {
  return filter.events.map(event => event.event_id)
}

export default function VidAngelExportPage() {
  const [titles, setTitles] = useState<VidAngelTitle[]>([])
  const [loadingCatalog, setLoadingCatalog] = useState(true)
  const [catalogError, setCatalogError] = useState<string | null>(null)
  const [selectedMediaId, setSelectedMediaId] = useState('')
  const [catalog, setCatalog] = useState<VidAngelFilterCatalog | null>(null)
  const [loadingFilters, setLoadingFilters] = useState(false)
  const [filtersError, setFiltersError] = useState<string | null>(null)
  const [searchTerm, setSearchTerm] = useState('')
  const [selectedEventIds, setSelectedEventIds] = useState<string[]>([])
  const [downloading, setDownloading] = useState(false)
  const [downloadResult, setDownloadResult] = useState<{ ok: boolean; message: string } | null>(null)

  useEffect(() => {
    let cancelled = false

    api.get<VidAngelCatalogResponse>('/api/vidangel/export/catalog')
      .then(data => {
        if (cancelled) return
        setTitles(Array.isArray(data.titles) ? data.titles : [])
        setCatalogError(null)
        setLoadingCatalog(false)
      })
      .catch(error => {
        if (cancelled) return
        setCatalogError(formatVidAngelError(error, 'Failed to load VidAngel export catalog'))
        setLoadingCatalog(false)
      })

    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    // When the title changes we reset the filter panel to the title's leaf set
    // so the default export starts from the complete VidAngel event list.
    if (!titles.length) return
    if (!selectedMediaId) {
      setSelectedMediaId(titles[0].media_id)
    }
  }, [selectedMediaId, titles])

  useEffect(() => {
    // Load the chosen title's leaf filters whenever the title changes so the
    // export list always matches the selected VidAngel work.
    let cancelled = false
    if (!selectedMediaId) {
      setCatalog(null)
      setSelectedEventIds([])
      setFiltersError(null)
      return
    }

    setLoadingFilters(true)
    setFiltersError(null)
    api.get<VidAngelFilterCatalog>(`/api/vidangel/export/titles/${encodeURIComponent(selectedMediaId)}/filters`)
      .then(data => {
        if (cancelled) return
        setCatalog(data)
        const allEventIds = data.categories.flatMap(category =>
          category.filters.flatMap(eventIdsForFilter),
        )
        setSelectedEventIds(allEventIds)
        setLoadingFilters(false)
      })
      .catch(error => {
        if (cancelled) return
        setCatalog(null)
        setSelectedEventIds([])
        setFiltersError(formatVidAngelError(error, 'Failed to load VidAngel filters'))
        setLoadingFilters(false)
      })

    return () => {
      cancelled = true
    }
  }, [selectedMediaId])

  const selectedCount = selectedEventIds.length
  const totalCount = catalog?.event_count ?? 0
  const normalizedSearch = searchTerm.trim().toLowerCase()

  const toggleEvent = (eventId: string) => {
    setSelectedEventIds(current =>
      current.includes(eventId)
        ? current.filter(id => id !== eventId)
        : [...current, eventId],
    )
  }

  const toggleFilter = (filter: VidAngelLeafFilter) => {
    const eventIds = eventIdsForFilter(filter)
    const allSelected = eventIds.every(eventId => selectedEventIds.includes(eventId))
    setSelectedEventIds(current => (
      allSelected
        ? current.filter(eventId => !eventIds.includes(eventId))
        : Array.from(new Set([...current, ...eventIds]))
    ))
  }

  const toggleCategory = (category: VidAngelCategory) => {
    const eventIds = category.filters.flatMap(eventIdsForFilter)
    const allSelected = eventIds.every(eventId => selectedEventIds.includes(eventId))
    setSelectedEventIds(current => (
      allSelected
        ? current.filter(eventId => !eventIds.includes(eventId))
        : Array.from(new Set([...current, ...eventIds]))
    ))
  }

  const clearSelection = () => {
    setSelectedEventIds([])
  }

  const selectAll = () => {
    if (!catalog) return
    setSelectedEventIds(catalog.categories.flatMap(category => category.filters.flatMap(eventIdsForFilter)))
  }

  const downloadSkp = async () => {
    if (!selectedMediaId || selectedEventIds.length === 0) return

    setDownloading(true)
    setDownloadResult(null)
    try {
      const text = await api.postText(
        `/api/vidangel/export/titles/${encodeURIComponent(selectedMediaId)}/skp`,
        { selected_event_ids: selectedEventIds },
      )
      const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
      const url = URL.createObjectURL(blob)
      const revokeObjectURL = typeof URL.revokeObjectURL === 'function' ? URL.revokeObjectURL.bind(URL) : null
      const link = document.createElement('a')
      const title = titles.find(item => item.media_id === selectedMediaId)?.title ?? selectedMediaId
      link.href = url
      link.download = `${sanitizeFilename(title)}.skp`
      document.body.appendChild(link)
      link.click()
      link.remove()
      window.setTimeout(() => revokeObjectURL?.(url), 0)
      setDownloadResult({ ok: true, message: `Downloaded ${selectedCount} selected event(s) as .skp.` })
    } catch (error) {
      setDownloadResult({ ok: false, message: formatVidAngelError(error, 'Failed to generate SKP export') })
    } finally {
      setDownloading(false)
    }
  }

  const visibleCategories = (catalog?.categories ?? []).flatMap(category => {
    const filters = category.filters.flatMap(filter => {
      if (!normalizedSearch) return [{ filter, visibleEvents: filter.events }]
      const filterHaystack = [
        filter.label,
        filter.description ?? '',
        filter.leaf_key,
        filter.category_label,
      ].join(' ').toLowerCase()
      if (filterHaystack.includes(normalizedSearch)) {
        return [{ filter, visibleEvents: filter.events }]
      }
      const visibleEvents = filter.events.filter(event => {
        const eventHaystack = [
          event.description,
          event.display_title ?? '',
          formatEventTime(event.start_ms),
          formatEventTime(event.end_ms),
        ].join(' ').toLowerCase()
        return eventHaystack.includes(normalizedSearch)
      })
      return visibleEvents.length > 0 ? [{ filter, visibleEvents }] : []
    })
    return filters.length > 0 ? [{ category, filters }] : []
  })

  const selectedTitle = titles.find(title => title.media_id === selectedMediaId) ?? null

  return (
    <div className="max-w-6xl space-y-6">
      <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <FileDown size={20} className="text-plex-orange" />
            <h1 className="text-2xl font-bold text-gray-100">VidAngel Export</h1>
          </div>
          <p className="mt-2 text-sm text-gray-500">
            Pick a VidAngel title, review every event, and export only the exact skips you want.
          </p>
        </div>

        <div className="rounded-xl border border-plex-border bg-plex-card px-4 py-3 text-sm text-gray-300">
          <div className="flex items-center gap-2 text-gray-100">
            <Download size={15} className="text-plex-orange" />
            <span>{selectedCount} / {totalCount} selected</span>
          </div>
          <div className="mt-1 text-xs text-gray-500">
            {selectedTitle ? selectedTitle.title : 'Choose a title to begin'}
          </div>
        </div>
      </div>

      {loadingCatalog ? (
        <div className="rounded-xl border border-plex-border bg-plex-card p-6 text-sm text-gray-500">
          Loading VidAngel export catalog...
        </div>
      ) : catalogError ? (
        <div className="rounded-xl border border-red-500/20 bg-red-500/10 p-6 text-sm text-red-300">
          {catalogError}
        </div>
      ) : titles.length === 0 ? (
        <div className="rounded-xl border border-plex-border bg-plex-card p-6 text-sm text-gray-400 space-y-2">
          <p>No VidAngel export catalog was found yet.</p>
          <p className="text-gray-500">
            Run the VidAngel export first so Leapfrog can load the title list and leaf filters.
          </p>
        </div>
      ) : (
        <div className="space-y-4">
          <div className="grid gap-4 lg:grid-cols-[320px_minmax(0,1fr)]">
            <div className="rounded-xl border border-plex-border bg-plex-card p-4">
              <label className="mb-2 block text-xs font-medium uppercase tracking-wide text-gray-500">
                VidAngel title
              </label>
              <select
                value={selectedMediaId}
                onChange={event => setSelectedMediaId(event.target.value)}
                className="w-full rounded-lg border border-plex-border bg-plex-darker px-3 py-2 text-sm text-gray-100 focus:border-plex-orange/60 focus:outline-none"
              >
                {titles.map(title => (
                  <option key={title.media_id} value={title.media_id}>
                    {title.title}
                  </option>
                ))}
              </select>

              <div className="mt-4 space-y-3 text-sm text-gray-400">
                <div className="rounded-lg border border-plex-border/80 bg-plex-darker/60 px-3 py-2">
                  <div className="text-xs text-gray-500">Available filters</div>
                  <div className="mt-1 text-gray-100">{catalog?.leaf_count ?? 0}</div>
                </div>
                <div className="rounded-lg border border-plex-border/80 bg-plex-darker/60 px-3 py-2">
                  <div className="text-xs text-gray-500">Event count</div>
                  <div className="mt-1 text-gray-100">{catalog?.event_count ?? 0}</div>
                </div>
                <div className="rounded-lg border border-plex-border/80 bg-plex-darker/60 px-3 py-2">
                  <div className="text-xs text-gray-500">Title type</div>
                  <div className="mt-1 text-gray-100">{selectedTitle?.media_type ?? 'Unknown'}</div>
                </div>
              </div>
            </div>

            <div className="rounded-xl border border-plex-border bg-plex-card p-4">
              <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
                <div>
                  <h2 className="text-lg font-semibold text-gray-100">Choose filters</h2>
                  <p className="text-sm text-gray-500">
                    Toggle whole groups or include and exclude individual VidAngel events.
                  </p>
                </div>
                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={selectAll}
                    disabled={!catalog || loadingFilters || selectedEventIds.length === totalCount}
                    className="rounded-lg border border-plex-border px-3 py-2 text-xs font-medium text-gray-300 transition-colors hover:bg-white/5 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    Select all
                  </button>
                  <button
                    type="button"
                    onClick={clearSelection}
                    disabled={loadingFilters || selectedEventIds.length === 0}
                    className="rounded-lg border border-plex-border px-3 py-2 text-xs font-medium text-gray-300 transition-colors hover:bg-white/5 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    Clear all
                  </button>
                  <button
                    type="button"
                    onClick={downloadSkp}
                    disabled={downloading || loadingFilters || selectedEventIds.length === 0}
                    className="inline-flex items-center gap-2 rounded-lg bg-plex-orange px-4 py-2 text-xs font-semibold text-white transition-colors hover:bg-plex-orange/90 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    {downloading ? <Loader2 size={14} className="animate-spin" /> : <Download size={14} />}
                    Generate `.skp`
                  </button>
                </div>
              </div>

              <label className="mt-4 flex items-center gap-2 rounded-lg border border-plex-border bg-plex-darker/70 px-3 py-2">
                <Search size={16} className="text-gray-500" />
                <input
                  value={searchTerm}
                  onChange={event => setSearchTerm(event.target.value)}
                  placeholder="Search filters, event descriptions, or timestamps"
                  className="w-full bg-transparent text-sm text-gray-100 placeholder:text-gray-600 focus:outline-none"
                />
              </label>

              {loadingFilters ? (
                <div className="mt-4 flex items-center gap-2 rounded-lg border border-plex-border/70 bg-black/10 px-4 py-3 text-sm text-gray-500">
                  <Loader2 size={14} className="animate-spin" />
                  Loading filters...
                </div>
              ) : filtersError ? (
                <div className="mt-4 rounded-lg border border-red-500/20 bg-red-500/10 px-4 py-3 text-sm text-red-300">
                  {filtersError}
                </div>
              ) : catalog && visibleCategories.length === 0 ? (
                <div className="mt-4 rounded-lg border border-plex-border/70 bg-black/10 px-4 py-3 text-sm text-gray-500">
                  No filters match the current search.
                </div>
              ) : (
                <div className="mt-4 space-y-4">
                  {visibleCategories.map(({ category, filters }) => {
                    const categoryEventIds = category.filters.flatMap(eventIdsForFilter)
                    const selectedInCategory = categoryEventIds.filter(eventId => selectedEventIds.includes(eventId)).length
                    const allSelected = selectedInCategory === categoryEventIds.length && categoryEventIds.length > 0
                    const partiallySelected = selectedInCategory > 0 && !allSelected
                    return (
                      <div key={category.key} className="rounded-xl border border-plex-border/70 bg-plex-darker/60 p-4">
                        <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
                          <div>
                            <div className="flex items-center gap-2">
                              <span className="text-sm font-medium text-gray-100">{category.label}</span>
                              <span className="rounded-full border border-plex-border px-2 py-0.5 text-[11px] text-gray-400">
                                {selectedInCategory}/{categoryEventIds.length} events
                              </span>
                            </div>
                            {category.description && (
                              <p className="mt-1 text-xs text-gray-500">{category.description}</p>
                            )}
                          </div>

                          <button
                            type="button"
                            onClick={() => toggleCategory(category)}
                            className="inline-flex items-center gap-2 rounded-lg border border-plex-border px-3 py-2 text-xs font-medium text-gray-300 transition-colors hover:bg-white/5"
                          >
                            {allSelected ? <CheckSquare2 size={14} /> : partiallySelected ? <Square size={14} /> : <Square size={14} />}
                            {allSelected ? 'Clear category' : 'Select category'}
                          </button>
                        </div>

                        <div className="mt-4 space-y-2">
                          {filters.map(({ filter, visibleEvents }) => {
                            const filterEventIds = eventIdsForFilter(filter)
                            const selectedInFilter = filterEventIds.filter(eventId => selectedEventIds.includes(eventId)).length
                            const checked = selectedInFilter === filterEventIds.length && filterEventIds.length > 0
                            return (
                              <div
                                key={filter.leaf_key}
                                className="rounded-lg border border-plex-border/60 bg-plex-card/60 px-3 py-3 text-sm text-gray-300"
                              >
                                <label className="flex items-start gap-3">
                                  <input
                                    type="checkbox"
                                    checked={checked}
                                    onChange={() => toggleFilter(filter)}
                                    className="mt-0.5 h-4 w-4 flex-shrink-0 accent-plex-orange"
                                  />
                                  <span className="min-w-0 flex-1">
                                  <span className="flex flex-wrap items-center gap-2">
                                    <span className="text-gray-100">{filter.label}</span>
                                    <span className="rounded-full border border-plex-border px-2 py-0.5 text-[11px] text-gray-500">
                                      {filter.leaf_key}
                                    </span>
                                    <span className="rounded-full border border-plex-border px-2 py-0.5 text-[11px] text-gray-500">
                                      {selectedInFilter}/{filter.event_count} events selected
                                    </span>
                                  </span>
                                  {filter.description && (
                                    <span className="mt-1 block text-xs text-gray-500">{filter.description}</span>
                                  )}
                                  </span>
                                </label>
                                <div className="mt-3 space-y-2 border-t border-plex-border/60 pt-3">
                                  {visibleEvents.map(event => {
                                    const eventChecked = selectedEventIds.includes(event.event_id)
                                    const timeRange = event.end_ms > event.start_ms
                                      ? `${formatEventTime(event.start_ms)} → ${formatEventTime(event.end_ms)}`
                                      : formatEventTime(event.start_ms)
                                    return (
                                      <label
                                        key={event.event_id}
                                        className="flex items-start gap-3 rounded-md bg-plex-darker/70 px-3 py-2"
                                      >
                                        <input
                                          type="checkbox"
                                          checked={eventChecked}
                                          onChange={() => toggleEvent(event.event_id)}
                                          className="mt-0.5 h-4 w-4 flex-shrink-0 accent-plex-orange"
                                        />
                                        <span className="min-w-0 flex-1">
                                          <span className="block font-mono text-xs text-plex-orange">{timeRange}</span>
                                          <span className="mt-1 block text-xs text-gray-300">{event.description}</span>
                                        </span>
                                      </label>
                                    )
                                  })}
                                </div>
                              </div>
                            )
                          })}
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          </div>

          {downloadResult && (
            <div className="rounded-xl border border-plex-border bg-plex-card px-4 py-3 text-sm text-gray-300">
              <span className="inline-flex items-center gap-2">
                {downloadResult.ok ? (
                  <CheckSquare2 size={14} className="text-green-400" />
                ) : (
                  <XCircle size={14} className="text-red-400" />
                )}
                {downloadResult.message}
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
