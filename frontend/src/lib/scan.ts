export function formatTimestamp(value?: string | null): string {
  if (!value) return ''
  const dt = new Date(value)
  if (Number.isNaN(dt.getTime())) return ''
  return dt.toLocaleString()
}

export function formatAnalysisState(value?: string): string {
  switch (value) {
    case 'scanned':
      return 'Fully scanned'
    case 'queued':
      return 'Queued'
    case 'scanning':
      return 'Scanning'
    case 'partially_scanned':
      return 'Partially scanned'
    case 'scanned_clean':
      return 'Scanned clean'
    case 'scanned_flagged':
      return 'Flagged'
    case 'failed':
      return 'Failed'
    case 'unscanned':
    default:
      return 'Unscanned'
  }
}

export function analysisStateClassName(value?: string): string {
  switch (value) {
    case 'scanned':
      return 'bg-emerald-500/15 text-emerald-300'
    case 'queued':
      return 'bg-blue-500/15 text-blue-300'
    case 'scanning':
      return 'bg-plex-orange/15 text-plex-orange'
    case 'partially_scanned':
      return 'bg-amber-500/15 text-amber-300'
    case 'scanned_clean':
      return 'bg-emerald-500/15 text-emerald-300'
    case 'scanned_flagged':
      return 'bg-red-500/15 text-red-300'
    case 'failed':
      return 'bg-red-500/15 text-red-400'
    case 'unscanned':
    default:
      return 'bg-gray-700/50 text-gray-400'
  }
}

export function scanStatusClassName(status?: string): string {
  switch (status) {
    case 'done':
      return 'bg-emerald-500/10 text-emerald-300 border-emerald-500/20'
    case 'running':
    case 'scanning':
      return 'bg-plex-orange/10 text-plex-orange border-plex-orange/20'
    case 'failed':
      return 'bg-red-500/10 text-red-300 border-red-500/20'
    case 'canceled':
      return 'bg-amber-500/10 text-amber-300 border-amber-500/20'
    case 'unavailable':
      return 'bg-gray-500/10 text-gray-300 border-gray-500/20'
    case 'pending':
    default:
      return 'bg-gray-500/10 text-gray-300 border-gray-500/20'
  }
}

export function formatStageKey(stageKey: string): string {
  return stageKey
    .split('_')
    .filter(Boolean)
    .map(part => part.toUpperCase() === 'CLIP' ? 'CLIP' : part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ')
}

export function shouldShowTitleInSegments(title: {
  segment_count: number
  analysis_state?: string
  status?: string
  queue_state?: string
  scan_statuses?: Record<string, { status?: string }>
}): boolean {
  if (title.segment_count > 0) {
    return true
  }
  if (title.analysis_state && title.analysis_state !== 'unscanned') {
    return true
  }
  if (title.queue_state === 'queued') {
    return true
  }
  if (title.status === 'scanning' || title.status === 'failed') {
    return true
  }
  return Object.values(title.scan_statuses ?? {}).some(entry => {
    const status = entry?.status ?? ''
    return status !== '' && status !== 'pending'
  })
}
