import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Library from '../../pages/Library'

vi.mock('../../api/client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

import { api } from '../../api/client'
const mockApi = api as {
  get: ReturnType<typeof vi.fn>
  post: ReturnType<typeof vi.fn>
}

const libraries = [
  { id: 'lib1', title: 'Movies', type: 'movie' },
]

const titles = [
  { plex_guid: 'g1', rating_key: '1', title: 'Movie A', status: 'done', progress: 1,
    finished_at: null, thumb_url: '', poster_url: '', show_guid: '', show_title: '',
    segment_count: 2, content_rating: 'R', media_type: 'movie', year: 2020, ignored: false,
    segment_counts_by_category: { nudity: 1, profanity: 1 },
    scan_statuses: {
      nudity: { status: 'done', source: 'nudenet', detail: 'Found 1 segment', updated_at: '2026-04-13T12:00:00' },
      profanity: { status: 'done', source: 'subtitles', detail: 'Found 1 segment', updated_at: '2026-04-13T12:00:00' },
    },
    analysis_state: 'scanned' },
  { plex_guid: 'g2', rating_key: '2', title: 'Movie B', status: 'pending', progress: 0,
    finished_at: null, thumb_url: '', poster_url: '', show_guid: '', show_title: '',
    segment_count: 0, content_rating: 'PG', media_type: 'movie', year: 2021, ignored: false,
    segment_counts_by_category: {},
    scan_statuses: {
      nudity: { status: 'pending', source: '', detail: 'Not scanned yet', updated_at: '2026-04-13T12:00:00' },
      profanity: { status: 'pending', source: '', detail: 'Not scanned yet', updated_at: '2026-04-13T12:00:00' },
    },
    analysis_state: 'unscanned' },
]

const segments = [
  {
    id: 1,
    plex_guid: 'g1',
    media_id: 'g1',
    title: 'Movie A',
    start_ms: 1000,
    end_ms: 5000,
    start_time: 1,
    end_time: 5,
    category: 'profanity',
    source: 'subtitles',
    confidence: 0.9,
    has_thumbnail: false,
    thumbnail_url: '',
    created_at: '2026-04-13T12:00:00',
    updated_at: '2026-04-13T12:00:00',
    text_excerpt: 'bad word here',
    review_status: 'pending',
    would_skip: true,
  },
]

const scannerIdle = {
  queue_size: 0, current_scan: null, current_title: null, current_progress: 0,
  current_scans: [], active_scans: [], workers_configured: 2,
  workers_active: 0, workers_idle: 2, paused: false,
}

function renderLibrary() {
  return render(<MemoryRouter><Library /></MemoryRouter>)
}

beforeEach(() => {
  mockApi.get.mockImplementation((path: string) => {
    if (path.includes('scanner-status')) return Promise.resolve(scannerIdle)
    if (path.includes('libraries') && !path.includes('titles')) return Promise.resolve({ libraries })
    if (path.includes('/segments')) return Promise.resolve({ segments })
    if (path.includes('titles')) return Promise.resolve({ titles })
    return Promise.resolve({})
  })
  mockApi.post.mockResolvedValue({ ok: true, synced: 2, new: 0 })
})

afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('Library', () => {
  it('renders the page heading', async () => {
    renderLibrary()
    await waitFor(() => expect(screen.getAllByText('Library').length).toBeGreaterThan(0))
  })

  it('shows library dropdown with loaded libraries', async () => {
    renderLibrary()
    await waitFor(() => expect(screen.getAllByText('Movies').length).toBeGreaterThan(0))
  })

  it('does NOT trigger sync automatically when a library is selected', async () => {
    renderLibrary()
    await waitFor(() => expect(screen.getAllByText('Movies').length).toBeGreaterThan(0))

    const select = screen.getByRole('combobox')
    await act(async () => {
      fireEvent.change(select, { target: { value: 'lib1' } })
    })
    await waitFor(() => screen.getByText('Movie A'))

    // No sync call should fire on selection
    const syncCalls = mockApi.post.mock.calls.filter(([path]: [string]) =>
      path.includes('/sync')
    )
    expect(syncCalls.length).toBe(0)
  })

  it('has an explicit "Sync from Plex" button that triggers sync on click', async () => {
    renderLibrary()
    await waitFor(() => expect(screen.getAllByText('Movies').length).toBeGreaterThan(0))
    const select = screen.getByRole('combobox')
    await act(async () => {
      fireEvent.change(select, { target: { value: 'lib1' } })
    })
    await waitFor(() => screen.getByText('Movie A'))

    const syncBtn = screen.getByRole('button', { name: /sync from plex/i })
    expect(syncBtn).toBeInTheDocument()

    await act(async () => {
      fireEvent.click(syncBtn)
    })

    await waitFor(() => {
      const syncCalls = mockApi.post.mock.calls.filter(([path]: [string]) =>
        path.includes('/sync')
      )
      expect(syncCalls.length).toBe(1)
    })
  })

  it('displays titles after library is selected', async () => {
    renderLibrary()
    await waitFor(() => expect(screen.getAllByText('Movies').length).toBeGreaterThan(0))
    const select = screen.getByRole('combobox')
    await act(async () => {
      fireEvent.change(select, { target: { value: 'lib1' } })
    })
    await waitFor(() => {
      expect(screen.getByText('Movie A')).toBeInTheDocument()
      expect(screen.getByText('Movie B')).toBeInTheDocument()
    })
  })

  it('renders scan status and category counts for a title', async () => {
    renderLibrary()
    await waitFor(() => expect(screen.getAllByText('Movies').length).toBeGreaterThan(0))
    const select = screen.getByRole('combobox')
    await act(async () => {
      fireEvent.change(select, { target: { value: 'lib1' } })
    })
    await waitFor(() => screen.getByText('Movie A'))
    expect(screen.getByText('Fully scanned')).toBeInTheDocument()
    expect(screen.getByText(/nudity: done/i)).toBeInTheDocument()
    expect(screen.getByText('profanity: 1')).toBeInTheDocument()
  })

  it('renders segment metadata when segments are expanded', async () => {
    renderLibrary()
    await waitFor(() => expect(screen.getAllByText('Movies').length).toBeGreaterThan(0))
    const select = screen.getByRole('combobox')
    await act(async () => {
      fireEvent.change(select, { target: { value: 'lib1' } })
    })
    await waitFor(() => screen.getByText('Movie A'))

    await act(async () => {
      fireEvent.click(document.querySelector('[title="Toggle segments"]') as Element)
    })

    await waitFor(() => expect(screen.getByText('profanity')).toBeInTheDocument())
    expect(screen.getByText('subtitles')).toBeInTheDocument()
    expect(screen.getByText('90% confidence')).toBeInTheDocument()
    expect(screen.getByText('Would skip')).toBeInTheDocument()
    expect(screen.getByText('“bad word here”')).toBeInTheDocument()
  })

  it('scan selected button fires requests with bounded concurrency', async () => {
    // Create 10 titles all in pending state
    const manyTitles = Array.from({ length: 10 }, (_, i) => ({
      ...titles[1], plex_guid: `g${i + 10}`, title: `Movie ${i}`, rating_key: `${i + 10}`,
    }))
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes('scanner-status')) return Promise.resolve(scannerIdle)
      if (path.includes('libraries') && !path.includes('titles')) return Promise.resolve({ libraries })
      if (path.includes('titles')) return Promise.resolve({ titles: manyTitles })
      return Promise.resolve({})
    })

    let maxConcurrent = 0
    let currentConcurrent = 0
    const pendingResolves: Array<() => void> = []
    mockApi.post.mockImplementation(() =>
      new Promise<{ ok: boolean }>(resolve => {
        currentConcurrent++
        maxConcurrent = Math.max(maxConcurrent, currentConcurrent)
        pendingResolves.push(() => {
          currentConcurrent--
          resolve({ ok: true })
        })
      })
    )

    renderLibrary()
    await waitFor(() => expect(screen.getAllByText('Movies').length).toBeGreaterThan(0))
    const select = screen.getByRole('combobox')
    await act(async () => {
      fireEvent.change(select, { target: { value: 'lib1' } })
    })
    await waitFor(() => screen.getByText('Movie 0'))

    const selectAllCheckbox = screen.getByRole('checkbox', { name: /select all filtered/i })
    await act(async () => {
      fireEvent.click(selectAllCheckbox)
    })

    const scanBtn = screen.getByRole('button', { name: /scan selected tonight/i })
    await act(async () => {
      fireEvent.click(scanBtn)
    })

    await waitFor(() => expect(mockApi.post).toHaveBeenCalledTimes(5))
    expect(maxConcurrent).toBeLessThanOrEqual(5)

    await act(async () => {
      pendingResolves.splice(0, 5).forEach(resolve => resolve())
    })

    await waitFor(() => expect(mockApi.post).toHaveBeenCalledTimes(10))
    expect(maxConcurrent).toBeLessThanOrEqual(5)

    await act(async () => {
      pendingResolves.splice(0).forEach(resolve => resolve())
    })

    await waitFor(() =>
      expect(screen.getByRole('button', { name: /scan selected tonight \(0\)/i })).toBeDisabled()
    )
  })

  it('aborts polling on unmount', async () => {
    const abortSpy = vi.spyOn(AbortController.prototype, 'abort')
    const { unmount } = renderLibrary()
    await waitFor(() => expect(mockApi.get).toHaveBeenCalled())
    const abortsBeforeUnmount = abortSpy.mock.calls.length
    unmount()
    expect(abortSpy.mock.calls.length).toBeGreaterThan(abortsBeforeUnmount)
    abortSpy.mockRestore()
  })
})
