import { useEffect, useState } from 'react'
import { Clapperboard, Scissors, VolumeX, AlertTriangle, CheckSquare2, Square } from 'lucide-react'
import { api } from '../api/client'
import { useCategoryDefinitions } from '../lib/categories'

interface RewriteSettings {
  selected_leaf_keys: string[]
  language_mode: 'mute' | 'cut'
  profile_user: string
  size_limit_percent: number
  output_container: string
  output_strategy: string
}

interface RewriteCandidate {
  media_id: string
  title: string
  file_path: string
  library_id: string
  library_title: string
  media_type: string
  year?: number | null
  sidecar_path: string
  source_type: string
  rewrite_ready: boolean
  unsupported_reason?: string | null
  available_leaf_count: number
  output_path: string
}

interface RewriteCandidatesResponse {
  settings: RewriteSettings
  candidates: RewriteCandidate[]
}

interface RewritePlan {
  media_id: string
  title: string
  selected_leaf_keys: string[]
  matched_leaf_keys: string[]
  language_mode: 'mute' | 'cut'
  summary: {
    cut_duration_seconds: number
    mute_duration_seconds: number
    kept_duration_seconds: number
    output_path: string
  }
}

interface RewriteJob {
  id: number
  status: string
  progress: number
  error?: string | null
  result?: {
    results?: {
      media_id: string
      title: string
      output_path: string
      output_size_bytes: number
    }[]
  } | null
}

interface UserRecord {
  username: string
  enabled: boolean
}

function formatError(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback
}

function secondsToClock(seconds: number): string {
  const rounded = Math.max(0, Math.round(seconds))
  const hours = Math.floor(rounded / 3600)
  const minutes = Math.floor((rounded % 3600) / 60)
  const secs = rounded % 60
  return `${hours}:${String(minutes).padStart(2, '0')}:${String(secs).padStart(2, '0')}`
}

export default function RewriteMediaPage() {
  const categories = useCategoryDefinitions()
  const [settings, setSettings] = useState<RewriteSettings | null>(null)
  const [candidates, setCandidates] = useState<RewriteCandidate[]>([])
  const [users, setUsers] = useState<UserRecord[]>([])
  const [selectedMediaIds, setSelectedMediaIds] = useState<string[]>([])
  const [selectedLeafKeys, setSelectedLeafKeys] = useState<string[]>([])
  const [profileUser, setProfileUser] = useState('')
  const [languageMode, setLanguageMode] = useState<'mute' | 'cut'>('mute')
  const [perMediaLanguageMode, setPerMediaLanguageMode] = useState<Record<string, 'mute' | 'cut'>>({})
  const [searchTerm, setSearchTerm] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [preview, setPreview] = useState<RewritePlan[]>([])
  const [previewLoading, setPreviewLoading] = useState(false)
  const [job, setJob] = useState<RewriteJob | null>(null)
  const [jobMessage, setJobMessage] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false

    Promise.all([
      api.get<RewriteCandidatesResponse>('/api/rewrite/candidates'),
      api.get<{ users: UserRecord[] }>('/api/users'),
    ])
      .then(([candidatePayload, usersPayload]) => {
        if (cancelled) return
        setSettings(candidatePayload.settings)
        setCandidates(Array.isArray(candidatePayload.candidates) ? candidatePayload.candidates : [])
        setSelectedLeafKeys(candidatePayload.settings.selected_leaf_keys ?? [])
        setProfileUser(candidatePayload.settings.profile_user ?? '')
        setLanguageMode(candidatePayload.settings.language_mode ?? 'mute')
        setUsers(Array.isArray(usersPayload.users) ? usersPayload.users : [])
        setLoading(false)
      })
      .catch(loadError => {
        if (cancelled) return
        setError(formatError(loadError, 'Failed to load rewrite candidates'))
        setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [])

  useEffect(() => {
    // Poll active rewrite jobs with a bounded loop so the page updates while
    // exports are running but never spins forever after terminal states.
    if (!job || job.status === 'completed' || job.status === 'failed') {
      return
    }
    let cancelled = false
    let attempts = 0
    let controller = new AbortController()

    const poll = async () => {
      attempts += 1
      controller.abort()
      controller = new AbortController()
      try {
        const nextJob = await api.get<RewriteJob>(`/api/rewrite/jobs/${job.id}`, { signal: controller.signal })
        if (cancelled) return
        setJob(nextJob)
        if (nextJob.status === 'completed') {
          setJobMessage('Rewrite export completed.')
          return
        }
        if (nextJob.status === 'failed') {
          setJobMessage(nextJob.error || 'Rewrite export failed.')
          return
        }
      } catch {
        if (cancelled) return
      }
      if (!cancelled && attempts < 240) {
        window.setTimeout(poll, 1000)
      }
    }

    window.setTimeout(poll, 1000)
    return () => {
      cancelled = true
      controller.abort()
    }
  }, [job])

  const readyCandidates = candidates.filter(candidate => candidate.rewrite_ready)
  const unsupportedCandidates = candidates.filter(candidate => !candidate.rewrite_ready)
  const normalizedSearch = searchTerm.trim().toLowerCase()

  const filteredLeaves = categories.map(category => ({
    ...category,
    labels: (category.labels ?? []).filter(label => {
      if (!normalizedSearch) return true
      const haystack = `${label.label} ${label.key} ${category.label}`.toLowerCase()
      return haystack.includes(normalizedSearch)
    }),
  })).filter(category => (category.labels?.length ?? 0) > 0)

  const toggleCandidate = (mediaId: string) => {
    setSelectedMediaIds(current => (
      current.includes(mediaId)
        ? current.filter(value => value !== mediaId)
        : [...current, mediaId]
    ))
  }

  const toggleLeaf = (leafKey: string) => {
    setSelectedLeafKeys(current => (
      current.includes(leafKey)
        ? current.filter(value => value !== leafKey)
        : [...current, leafKey]
    ))
  }

  const toggleCategory = (leafKeys: string[]) => {
    const allSelected = leafKeys.every(leafKey => selectedLeafKeys.includes(leafKey))
    setSelectedLeafKeys(current => (
      allSelected
        ? current.filter(value => !leafKeys.includes(value))
        : Array.from(new Set([...current, ...leafKeys]))
    ))
  }

  const loadProfileLeafKeys = async () => {
    if (!profileUser) return
    const payload = await api.get<{ selected_leaf_keys: string[] }>(`/api/rewrite/profile-leaf-keys/${encodeURIComponent(profileUser)}`)
    setSelectedLeafKeys(payload.selected_leaf_keys ?? [])
  }

  const saveDefaults = async () => {
    const nextSettings = await api.put<RewriteSettings>('/api/rewrite/settings', {
      selected_leaf_keys: selectedLeafKeys,
      language_mode: languageMode,
      profile_user: profileUser,
    })
    setSettings(nextSettings)
    setJobMessage('Saved rewrite defaults.')
  }

  const previewRewrite = async () => {
    if (selectedMediaIds.length === 0 || selectedLeafKeys.length === 0) return
    setPreviewLoading(true)
    setJobMessage(null)
    try {
      const payload = await api.post<{
        plans: RewritePlan[]
      }>('/api/rewrite/plan', {
        media_ids: selectedMediaIds,
        selected_leaf_keys: selectedLeafKeys,
        profile_user: profileUser || undefined,
        language_mode: languageMode,
        per_media_language_mode: perMediaLanguageMode,
      })
      setPreview(payload.plans ?? [])
    } catch (previewError) {
      setJobMessage(formatError(previewError, 'Failed to build rewrite preview'))
    } finally {
      setPreviewLoading(false)
    }
  }

  const startRewrite = async () => {
    if (selectedMediaIds.length === 0 || selectedLeafKeys.length === 0) return
    setJobMessage(null)
    try {
      const payload = await api.post<{ job_id: number; status: string }>('/api/rewrite/jobs', {
        media_ids: selectedMediaIds,
        selected_leaf_keys: selectedLeafKeys,
        profile_user: profileUser || undefined,
        language_mode: languageMode,
        per_media_language_mode: perMediaLanguageMode,
        size_limit_percent: settings?.size_limit_percent ?? 120,
      })
      setJob({
        id: payload.job_id,
        status: payload.status,
        progress: 0,
      })
      setJobMessage(`Rewrite job ${payload.job_id} started.`)
    } catch (startError) {
      setJobMessage(formatError(startError, 'Failed to start rewrite job'))
    }
  }

  if (loading) {
    return <div className="rounded-xl border border-plex-border bg-plex-card p-6 text-sm text-gray-500">Loading rewrite workflow...</div>
  }

  if (error) {
    return <div className="rounded-xl border border-red-500/20 bg-red-500/10 p-6 text-sm text-red-300">{error}</div>
  }

  return (
    <div className="max-w-7xl space-y-6">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-end lg:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Clapperboard size={20} className="text-plex-orange" />
            <h1 className="text-2xl font-bold text-gray-100">Rewrite Media</h1>
          </div>
          <p className="mt-2 text-sm text-gray-500">
            Build sibling MKV exports from VidAngel leaf filters, with language ranges muted or cut and all other selected ranges cut.
          </p>
        </div>
        <div className="rounded-xl border border-plex-border bg-plex-card px-4 py-3 text-sm text-gray-300">
          <div>{selectedMediaIds.length} title(s) selected</div>
          <div>{selectedLeafKeys.length} leaf filter(s) selected</div>
          <div className="text-xs text-gray-500">Output: sibling `.cleaned.mkv`, size cap {settings?.size_limit_percent ?? 120}%</div>
        </div>
      </div>

      <div className="grid gap-4 xl:grid-cols-[340px_minmax(0,1fr)]">
        <div className="space-y-4">
          <section className="rounded-xl border border-plex-border bg-plex-card p-4">
            <h2 className="text-lg font-semibold text-gray-100">Rewrite defaults</h2>
            <div className="mt-4 space-y-3 text-sm">
              <label className="block">
                <span className="mb-1 block text-xs uppercase tracking-wide text-gray-500">Saved user profile</span>
                <select
                  value={profileUser}
                  onChange={event => setProfileUser(event.target.value)}
                  className="w-full rounded-lg border border-plex-border bg-plex-darker px-3 py-2 text-gray-100"
                >
                  <option value="">No profile shortcut</option>
                  {users.map(user => (
                    <option key={user.username} value={user.username}>{user.username}</option>
                  ))}
                </select>
              </label>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={loadProfileLeafKeys}
                  disabled={!profileUser}
                  className="rounded-lg border border-plex-border px-3 py-2 text-xs font-medium text-gray-300 disabled:opacity-50"
                >
                  Load profile filters
                </button>
                <button
                  type="button"
                  onClick={saveDefaults}
                  className="rounded-lg border border-plex-border px-3 py-2 text-xs font-medium text-gray-300"
                >
                  Save defaults
                </button>
              </div>
              <div>
                <div className="mb-1 text-xs uppercase tracking-wide text-gray-500">Language handling default</div>
                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => setLanguageMode('mute')}
                    className={`rounded-lg px-3 py-2 text-xs font-medium ${languageMode === 'mute' ? 'bg-plex-orange text-white' : 'border border-plex-border text-gray-300'}`}
                  >
                    Mute language
                  </button>
                  <button
                    type="button"
                    onClick={() => setLanguageMode('cut')}
                    className={`rounded-lg px-3 py-2 text-xs font-medium ${languageMode === 'cut' ? 'bg-plex-orange text-white' : 'border border-plex-border text-gray-300'}`}
                  >
                    Cut language
                  </button>
                </div>
              </div>
            </div>
          </section>

          <section className="rounded-xl border border-plex-border bg-plex-card p-4">
            <div className="flex items-center justify-between">
              <h2 className="text-lg font-semibold text-gray-100">Titles</h2>
              <span className="text-xs text-gray-500">{readyCandidates.length} rewrite-ready</span>
            </div>
            <div className="mt-4 space-y-2 max-h-[34rem] overflow-y-auto">
              {readyCandidates.map(candidate => {
                const checked = selectedMediaIds.includes(candidate.media_id)
                const languageOverride = perMediaLanguageMode[candidate.media_id] ?? languageMode
                return (
                  <div key={candidate.media_id} className="rounded-lg border border-plex-border/70 bg-plex-darker/60 p-3">
                    <label className="flex items-start gap-3">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggleCandidate(candidate.media_id)}
                        className="mt-1 h-4 w-4 accent-plex-orange"
                      />
                      <span className="min-w-0 flex-1">
                        <span className="block text-sm font-medium text-gray-100">{candidate.title}</span>
                        <span className="block text-xs text-gray-500">{candidate.source_type} • {candidate.available_leaf_count} leaves</span>
                        <span className="block truncate text-[11px] text-gray-600">{candidate.output_path}</span>
                      </span>
                    </label>
                    {checked && (
                      <div className="mt-3 flex items-center gap-2 text-xs">
                        <span className="text-gray-500">Language override</span>
                        <button
                          type="button"
                          onClick={() => setPerMediaLanguageMode(current => ({ ...current, [candidate.media_id]: 'mute' }))}
                          className={`rounded-lg px-2 py-1 ${languageOverride === 'mute' ? 'bg-plex-orange text-white' : 'border border-plex-border text-gray-300'}`}
                        >
                          <VolumeX size={12} className="inline mr-1" />
                          Mute
                        </button>
                        <button
                          type="button"
                          onClick={() => setPerMediaLanguageMode(current => ({ ...current, [candidate.media_id]: 'cut' }))}
                          className={`rounded-lg px-2 py-1 ${languageOverride === 'cut' ? 'bg-plex-orange text-white' : 'border border-plex-border text-gray-300'}`}
                        >
                          <Scissors size={12} className="inline mr-1" />
                          Cut
                        </button>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </section>

          {unsupportedCandidates.length > 0 && (
            <section className="rounded-xl border border-yellow-500/20 bg-yellow-500/10 p-4">
              <div className="flex items-center gap-2 text-yellow-200">
                <AlertTriangle size={16} />
                <h2 className="text-sm font-semibold">Unsupported titles</h2>
              </div>
              <div className="mt-3 space-y-2 text-xs text-yellow-100/90">
                {unsupportedCandidates.slice(0, 8).map(candidate => (
                  <div key={candidate.media_id}>
                    <span className="font-medium">{candidate.title}</span>: {candidate.unsupported_reason}
                  </div>
                ))}
              </div>
            </section>
          )}
        </div>

        <div className="space-y-4">
          <section className="rounded-xl border border-plex-border bg-plex-card p-4">
            <div className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
              <div>
                <h2 className="text-lg font-semibold text-gray-100">VidAngel leaf filters</h2>
                <p className="text-sm text-gray-500">All rewrite filtering uses the VidAngel taxonomy, including non-VidAngel sidecar inputs.</p>
              </div>
              <input
                value={searchTerm}
                onChange={event => setSearchTerm(event.target.value)}
                placeholder="Search leaves"
                className="w-full md:w-64 rounded-lg border border-plex-border bg-plex-darker px-3 py-2 text-sm text-gray-100"
              />
            </div>
            <div className="mt-4 space-y-4 max-h-[34rem] overflow-y-auto">
              {filteredLeaves.map(category => {
                const leafKeys = (category.labels ?? []).map(label => label.key)
                const allSelected = leafKeys.length > 0 && leafKeys.every(leafKey => selectedLeafKeys.includes(leafKey))
                return (
                  <div key={category.key} className="rounded-xl border border-plex-border/70 bg-plex-darker/60 p-4">
                    <div className="flex items-center justify-between gap-2">
                      <div>
                        <div className="text-sm font-medium text-gray-100">{category.label}</div>
                        <div className="text-xs text-gray-500">{category.description}</div>
                      </div>
                      <button
                        type="button"
                        onClick={() => toggleCategory(leafKeys)}
                        className="inline-flex items-center gap-2 rounded-lg border border-plex-border px-3 py-2 text-xs text-gray-300"
                      >
                        {allSelected ? <CheckSquare2 size={14} /> : <Square size={14} />}
                        {allSelected ? 'Clear' : 'Select'}
                      </button>
                    </div>
                    <div className="mt-4 grid gap-2 lg:grid-cols-2">
                      {(category.labels ?? []).map(label => (
                        <label key={label.key} className="flex items-start gap-3 rounded-lg border border-plex-border/60 bg-plex-card/60 px-3 py-2 text-sm text-gray-300">
                          <input
                            type="checkbox"
                            checked={selectedLeafKeys.includes(label.key)}
                            onChange={() => toggleLeaf(label.key)}
                            className="mt-0.5 h-4 w-4 accent-plex-orange"
                          />
                          <span className="min-w-0 flex-1">
                            <span className="block text-gray-100">{label.label}</span>
                            <span className="block text-[11px] text-gray-500">{label.key}</span>
                          </span>
                        </label>
                      ))}
                    </div>
                  </div>
                )
              })}
            </div>
          </section>

          <section className="rounded-xl border border-plex-border bg-plex-card p-4">
            <div className="flex flex-wrap gap-2">
              <button
                type="button"
                onClick={previewRewrite}
                disabled={selectedMediaIds.length === 0 || selectedLeafKeys.length === 0 || previewLoading}
                className="rounded-lg bg-plex-orange px-4 py-2 text-xs font-semibold text-white disabled:opacity-50"
              >
                {previewLoading ? 'Previewing...' : 'Preview rewrite'}
              </button>
              <button
                type="button"
                onClick={startRewrite}
                disabled={selectedMediaIds.length === 0 || selectedLeafKeys.length === 0}
                className="rounded-lg border border-plex-border px-4 py-2 text-xs font-semibold text-gray-300 disabled:opacity-50"
              >
                Start rewrite job
              </button>
            </div>
            {job && (
              <div className="mt-4 rounded-lg border border-plex-border/70 bg-black/10 px-4 py-3 text-sm text-gray-300">
                <div>Job #{job.id} • {job.status}</div>
                <div className="mt-1 text-xs text-gray-500">Progress: {job.progress}%</div>
                {job.result?.results && (
                  <div className="mt-2 space-y-1 text-xs text-gray-400">
                    {job.result.results.map(result => (
                      <div key={result.media_id}>{result.title}: {result.output_path}</div>
                    ))}
                  </div>
                )}
              </div>
            )}
            {jobMessage && (
              <div className="mt-3 text-sm text-gray-300">{jobMessage}</div>
            )}
          </section>

          {preview.length > 0 && (
            <section className="rounded-xl border border-plex-border bg-plex-card p-4">
              <h2 className="text-lg font-semibold text-gray-100">Preview</h2>
              <div className="mt-4 space-y-3">
                {preview.map(plan => (
                  <div key={plan.media_id} className="rounded-lg border border-plex-border/70 bg-plex-darker/60 p-3 text-sm text-gray-300">
                    <div className="font-medium text-gray-100">{plan.title}</div>
                    <div className="mt-1 text-xs text-gray-500">
                      Cut {secondsToClock(plan.summary.cut_duration_seconds)} • Mute {secondsToClock(plan.summary.mute_duration_seconds)} • Keep {secondsToClock(plan.summary.kept_duration_seconds)}
                    </div>
                    <div className="mt-1 text-xs text-gray-600">{plan.summary.output_path}</div>
                  </div>
                ))}
              </div>
            </section>
          )}
        </div>
      </div>
    </div>
  )
}
