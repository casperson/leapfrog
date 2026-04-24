import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Segments from '../../pages/Segments'

vi.mock('../../api/client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    delete: vi.fn(),
  },
}))

import { api } from '../../api/client'

const mockApi = api as {
  get: ReturnType<typeof vi.fn>
  post: ReturnType<typeof vi.fn>
  delete: ReturnType<typeof vi.fn>
}

const categories = [
  { key: 'nudity', label: 'Nudity', description: 'Skip detected nudity scenes.', default_threshold: 0.6 },
  { key: 'sexual_content', label: 'Sexual Content', description: 'Skip detected sexual activity or suggestive intimate scenes.', default_threshold: 0.55 },
  { key: 'profanity', label: 'Profanity', description: 'Skip subtitle or transcript profanity matches.', default_threshold: 0.5 },
  { key: 'violence', label: 'Violence', description: 'Skip detected violence, blood, or weapon scenes.', default_threshold: 0.55 },
  { key: 'drugs', label: 'Drugs', description: 'Skip detected drug use or paraphernalia scenes.', default_threshold: 0.55 },
]

const libraries = [
  { id: 'lib1', title: 'Movies', type: 'movie' },
]

const titles = [
  {
    plex_guid: 'g-partial',
    title: 'Movie Partial',
    status: 'done',
    progress: 1,
    finished_at: '2026-04-19T10:00:00',
    thumb_url: '',
    segment_count: 0,
    analysis_state: 'partially_scanned',
    queue_state: 'idle',
    scan_statuses: {
      nudity: { status: 'done', source: 'nudenet', detail: 'No nudity detected', updated_at: '2026-04-19T10:00:00' },
      violence: { status: 'failed', source: 'semantic_clip', detail: 'Model unavailable', updated_at: '2026-04-19T10:01:00' },
    },
    segment_counts_by_category: { nudity: 0, violence: 0 },
  },
  {
    plex_guid: 'g-flagged',
    title: 'Movie Flagged',
    status: 'done',
    progress: 1,
    finished_at: '2026-04-19T11:00:00',
    thumb_url: '',
    segment_count: 1,
    analysis_state: 'scanned_flagged',
    queue_state: 'idle',
    scan_statuses: {
      profanity: { status: 'done', source: 'subtitles', detail: '1 match', updated_at: '2026-04-19T11:00:00' },
    },
    segment_counts_by_category: { profanity: 1 },
  },
]

const flaggedSegments = {
  segments: [
    {
      id: 10,
      plex_guid: 'g-flagged',
      media_id: 'g-flagged',
      title: 'Movie Flagged',
      start_ms: 1000,
      end_ms: 4000,
      start_time: 1,
      end_time: 4,
      category: 'profanity',
      source: 'subtitles',
      confidence: 0.92,
      has_thumbnail: false,
      thumbnail_url: '',
      created_at: '2026-04-19T11:00:00',
      labels: 'fuck',
      text_excerpt: 'what the fuck',
      review_status: 'pending',
      would_skip: true,
    },
  ],
}

const partialDetail = {
  media_id: 'g-partial',
  title: 'Movie Partial',
  analysis_state: 'partially_scanned',
  segment_count: 0,
  segment_counts_by_category: {
    nudity: 0,
    sexual_content: 0,
    profanity: 0,
    violence: 0,
    drugs: 0,
  },
  scan_statuses: [
    { category: 'nudity', status: 'done', source: 'nudenet', detail: 'No nudity detected', segment_count: 0, progress: 1, updated_at: '2026-04-19T10:00:00' },
    { category: 'violence', status: 'failed', source: 'semantic_clip', detail: 'Model unavailable', segment_count: 0, progress: 1, updated_at: '2026-04-19T10:01:00' },
  ],
  stage_statuses: [
    { stage_key: 'prepare', status: 'done', source: 'scanner', detail: 'Prepared local scan context', progress: 1, created_at: '2026-04-19T10:00:00', updated_at: '2026-04-19T10:00:00' },
    { stage_key: 'violence', category: 'violence', status: 'failed', source: 'semantic_clip', detail: 'Model unavailable', progress: 1, created_at: '2026-04-19T10:01:00', updated_at: '2026-04-19T10:01:00' },
  ],
  last_scan_time: '2026-04-19T10:01:00',
  preference_resolution_success: true,
  effective_segment_count: 0,
  queue: { state: 'idle', position: null, priority: 0, cancel_requested: false },
  job_status: 'done',
}

const flaggedDetail = {
  ...partialDetail,
  media_id: 'g-flagged',
  title: 'Movie Flagged',
  analysis_state: 'scanned_flagged',
  segment_count: 1,
  segment_counts_by_category: {
    nudity: 0,
    sexual_content: 0,
    profanity: 1,
    violence: 0,
    drugs: 0,
  },
  scan_statuses: [
    { category: 'profanity', status: 'done', source: 'subtitles', detail: '1 subtitle match', segment_count: 1, progress: 1, updated_at: '2026-04-19T11:00:00' },
  ],
  stage_statuses: [
    { stage_key: 'prepare', status: 'done', source: 'scanner', detail: 'Prepared local scan context', progress: 1, created_at: '2026-04-19T11:00:00', updated_at: '2026-04-19T11:00:00' },
    { stage_key: 'profanity', category: 'profanity', status: 'done', source: 'subtitles', detail: 'Matched 1 profanity root', progress: 1, created_at: '2026-04-19T11:00:00', updated_at: '2026-04-19T11:00:00' },
  ],
  last_scan_time: '2026-04-19T11:00:00',
  effective_segment_count: 1,
}

const scannerIdle = {
  queue_size: 0,
  current_scan: null,
  current_title: null,
  current_progress: 0,
  current_scans: [],
  active_scans: [],
  workers_configured: 2,
  workers_active: 0,
  workers_idle: 2,
  paused: false,
}

function renderSegments() {
  return render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <Segments />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  mockApi.get.mockImplementation((path: string) => {
    if (path.includes('/api/settings/categories')) return Promise.resolve({ categories })
    if (path.includes('/api/users')) return Promise.resolve({ users: [{ username: 'alice', enabled: true }] })
    if (path.includes('/api/libraries') && !path.includes('/titles')) return Promise.resolve({ libraries })
    if (path.includes('/api/sessions/scanner-status')) return Promise.resolve(scannerIdle)
    if (path.includes('/api/libraries/lib1/titles')) return Promise.resolve({ titles })
    if (path.includes('/api/titles/g-partial/segments')) return Promise.resolve({ segments: [] })
    if (path.includes('/api/titles/g-flagged/segments')) return Promise.resolve(flaggedSegments)
    if (path.includes('/api/titles/g-partial/scan-details')) return Promise.resolve(partialDetail)
    if (path.includes('/api/titles/g-flagged/scan-details')) return Promise.resolve(flaggedDetail)
    return Promise.resolve({})
  })
  mockApi.post.mockResolvedValue({ ok: true })
  mockApi.delete.mockResolvedValue({ ok: true })
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('Segments page', () => {
  it('shows partially scanned titles even when no saved segments exist', async () => {
    renderSegments()

    await waitFor(() => expect(screen.getAllByText('Movies').length).toBeGreaterThan(0))
    await act(async () => {
      fireEvent.click(screen.getAllByText('Movies')[0])
    })

    await waitFor(() => expect(screen.getByText('Movie Partial')).toBeInTheDocument())
    expect(screen.getByText('Partially scanned')).toBeInTheDocument()
  })

  it('filters visible titles by movie name', async () => {
    renderSegments()

    await waitFor(() => expect(screen.getAllByText('Movies').length).toBeGreaterThan(0))
    await act(async () => {
      fireEvent.click(screen.getAllByText('Movies')[0])
    })

    await waitFor(() => expect(screen.getByText('Movie Partial')).toBeInTheDocument())
    await act(async () => {
      fireEvent.change(screen.getAllByPlaceholderText('Filter titles…')[0], { target: { value: 'Flagged' } })
    })

    expect(screen.queryByText('Movie Partial')).not.toBeInTheDocument()
    expect(screen.getByText('Movie Flagged')).toBeInTheDocument()
  })

  it('renders scan detail timeline for a selected title', async () => {
    renderSegments()

    await waitFor(() => expect(screen.getAllByText('Movies').length).toBeGreaterThan(0))
    await act(async () => {
      fireEvent.click(screen.getAllByText('Movies')[0])
    })

    await waitFor(() => expect(screen.getByText('Movie Partial')).toBeInTheDocument())
    await act(async () => {
      fireEvent.click(screen.getByText('Movie Partial'))
    })

    await waitFor(() => expect(screen.getByText('Detector Timeline')).toBeInTheDocument())
    expect(screen.getAllByText('Violence').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Model unavailable').length).toBeGreaterThan(0)
  })

  it('renders segment metadata including skip decision', async () => {
    renderSegments()

    await waitFor(() => expect(screen.getAllByText('Movies').length).toBeGreaterThan(0))
    await act(async () => {
      fireEvent.click(screen.getAllByText('Movies')[0])
    })

    await waitFor(() => expect(screen.getByText('Movie Flagged')).toBeInTheDocument())
    await act(async () => {
      fireEvent.click(screen.getByText('Movie Flagged'))
    })

    await waitFor(() => expect(screen.getAllByText('Profanity').length).toBeGreaterThan(0))
    expect(screen.getAllByText('subtitles').length).toBeGreaterThan(0)
    expect(screen.getByText('Would skip')).toBeInTheDocument()
    expect(screen.getByText('“what the fuck”')).toBeInTheDocument()
  })
})
