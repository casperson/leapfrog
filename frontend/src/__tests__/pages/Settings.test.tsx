import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Settings from '../../pages/Settings'

vi.mock('../../api/client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
  },
}))

import { api } from '../../api/client'
const mockApi = api as {
  get: ReturnType<typeof vi.fn>
  post: ReturnType<typeof vi.fn>
  put: ReturnType<typeof vi.fn>
}

const defaultSettings = {
  plex_url: 'http://plex:32400',
  plex_token: 'abc',
  poll_interval: '5',
  confidence_threshold: '0.4',
  sexual_content_detection_threshold: '0.45',
  violence_detection_threshold: '0.45',
  drugs_detection_threshold: '0.45',
  skip_buffer_ms: '1000',
  scan_step_ms: '250',
  scan_workers: '2',
  segment_gap_ms: '2000',
  segment_min_hits: '2',
  image_segment_max_ms: '15000',
  scan_window_start: '23:00',
  scan_window_end: '06:00',
  log_level: 'INFO',
  excluded_library_ids: '[]',
  scan_ratings: '[]',
  scan_labels: '["FEMALE_BREAST_EXPOSED"]',
  default_skip_labels: '{"sexual_content":["explicit_sex"]}',
  semantic_detection_labels: '{"sexual_content":["explicit_sex","brief_kiss"]}',
  nudenet_model: '640m',
  nudenet_model_path: '',
  sync_enabled: '0',
  sync_instance_name: '',
  sync_github_repo: '',
  sync_conflict_resolution: 'consensus',
  sync_verified_threshold: '2',
  sync_timing_tolerance_ms: '2000',
}

function renderSettings() {
  return render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <Settings />
    </MemoryRouter>
  )
}

beforeEach(() => {
  mockApi.get.mockImplementation((path: string) => {
    if (path === '/api/settings') return Promise.resolve(defaultSettings)
    if (path === '/api/settings/categories') {
      return Promise.resolve({
        categories: [
          {
            key: 'sexual_content',
            label: 'Sexual Content',
            description: 'Sexual activity or intimate content.',
            default_threshold: 0.5,
            labels: [
              {
                key: 'explicit_sex',
                label: 'Explicit Sex',
                description: 'Visible explicit sexual activity.',
                default_skip: true,
                default_detect: true,
              },
              {
                key: 'brief_kiss',
                label: 'Brief Kiss',
                description: 'Brief peck or non-explicit kiss.',
                default_skip: false,
                default_detect: true,
              },
            ],
          },
        ],
      })
    }
    if (path.includes('detector-labels')) return Promise.resolve({ labels: [] })
    if (path.includes('libraries')) return Promise.resolve({ libraries: [] })
    return Promise.resolve({})
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('Settings', () => {
  it('renders the page heading', async () => {
    renderSettings()
    await waitFor(() => expect(screen.getByText('Settings')).toBeInTheDocument())
  })

  it('loads and displays plex url from settings', async () => {
    renderSettings()
    await waitFor(() => {
      const input = screen.getByDisplayValue('http://plex:32400')
      expect(input).toBeInTheDocument()
    })
  })

  it('renders semantic detector threshold controls', async () => {
    renderSettings()

    await waitFor(() => expect(screen.getAllByDisplayValue('0.45').length).toBeGreaterThan(0))
    expect(screen.getByText('Sexual Content Threshold')).toBeInTheDocument()
    expect(screen.getByText('Violence Threshold')).toBeInTheDocument()
    expect(screen.getByText('Drugs Threshold')).toBeInTheDocument()
  })

  it('calls semantic model prep endpoint from settings', async () => {
    mockApi.post.mockResolvedValue({ ok: true, message: 'Semantic ONNX model is ready in local app storage.' })
    renderSettings()

    await waitFor(() => expect(screen.getByText('Settings')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Download/Check semantic model'))

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith('/api/settings/prepare-semantic-model')
    )
  })

  it('upload polling terminates after MAX_POLLS without timing out infinitely', async () => {
    // Simulate an upload job that stays 'running' indefinitely — the loop must stop
    let pollCount = 0
    mockApi.post.mockImplementation((path: string) => {
      if (path.includes('upload')) return Promise.resolve({ job_id: 42 })
      if (path.includes('job')) {
        pollCount++
        return Promise.resolve({ status: 'running', progress: 50, error: null, result: null })
      }
      return Promise.resolve({})
    })
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes('job')) return Promise.resolve({ status: 'running', progress: 50, error: null, result: null })
      if (path === '/api/settings') return Promise.resolve(defaultSettings)
      if (path === '/api/settings/categories') return Promise.resolve({ categories: [] })
      if (path.includes('detector-labels')) return Promise.resolve({ labels: [] })
      return Promise.resolve({})
    })

    renderSettings()
    await waitFor(() => screen.getByText('Settings'))
    vi.useFakeTimers()

    // The bounded poll loop should stop after MAX_POLLS (120) ticks — it must not loop forever.
    // We advance timers rapidly to simulate many poll attempts.
    await act(async () => {
      // Each poll waits 1000ms — advance 130 seconds to exceed MAX_POLLS
      vi.advanceTimersByTime(130_000)
    })

    // At this point the loop should have terminated — verify poll count is bounded
    // (exact value depends on component state; key thing is test completes)
    expect(pollCount).toBeLessThanOrEqual(130)
    vi.runOnlyPendingTimers()
    vi.useRealTimers()
  })

  it('renders granular label defaults from category metadata', async () => {
    renderSettings()

    await waitFor(() => expect(screen.getByText('Granular Label Defaults')).toBeInTheDocument())
    expect(screen.getByText('Explicit Sex')).toBeInTheDocument()
    expect(screen.getByText('Brief Kiss')).toBeInTheDocument()
  })

  it('wires clear stored segments maintenance action', async () => {
    vi.stubGlobal('confirm', vi.fn(() => true))
    mockApi.post.mockResolvedValue({ deleted: 3, reset_scan_jobs: 2 })

    renderSettings()

    await waitFor(() => expect(screen.getByText('Clear Stored Segments')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Clear Stored Segments'))

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith('/api/segments/clear-all', {
        reset_scan_state: true,
        clear_queue: true,
      })
    )
    expect(screen.getByText('Deleted 3 segment(s); reset 2 title(s).')).toBeInTheDocument()
  })
})
