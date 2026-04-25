import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import Dashboard from '../../pages/Dashboard'

// Mock the api module so tests don't make real HTTP calls
vi.mock('../../api/client', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

import { api } from '../../api/client'
const mockApi = api as { get: ReturnType<typeof vi.fn>; post: ReturnType<typeof vi.fn> }

const emptySessions = { sessions: [] }
const emptyEvents = { events: [] }
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
  skipper: {
    healthy: true,
    status: 'active',
    last_poll_at: '2026-04-25T09:00:00',
    last_success_at: '2026-04-25T09:00:00',
    last_skip_at: null,
    last_error: null,
    last_error_at: null,
    last_session_count: 0,
    last_seek_success_at: null,
    last_seek_failure: null,
  },
}

const queueSnapshot = {
  jobs: [],
  queue_size: 0,
  current: null,
  currents: [],
  active_scans: [],
  paused: false,
}

function renderDashboard() {
  return render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <Dashboard />
    </MemoryRouter>
  )
}

beforeEach(() => {
  mockApi.get.mockImplementation((path: string) => {
    if (path.includes('events')) return Promise.resolve(emptyEvents)
    if (path.includes('/api/scan/queue')) return Promise.resolve(queueSnapshot)
    if (path.includes('scanner-status')) return Promise.resolve(scannerIdle)
    return Promise.resolve(emptySessions)
  })
})

afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
})

describe('Dashboard', () => {
  it('renders the page heading', async () => {
    renderDashboard()
    await waitFor(() => expect(screen.getByText('Dashboard')).toBeInTheDocument())
  })

  it('shows "No active streams" when sessions list is empty', async () => {
    renderDashboard()
    await waitFor(() => expect(screen.getByText('No active streams')).toBeInTheDocument())
  })

  it('shows "No skips yet" when events list is empty', async () => {
    renderDashboard()
    await waitFor(() => expect(screen.getByText('No skips yet')).toBeInTheDocument())
  })

  it('displays active session info', async () => {
    const session = {
      session_key: 's1',
      user: 'alice',
      title: 'Inception',
      media_type: 'movie',
      position_ms: 30000,
      duration_ms: 7200000,
      client: 'Plex Web',
      is_controllable: true,
      filtering_enabled: true,
      thumb_url: '',
    }
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes('events')) return Promise.resolve(emptyEvents)
      if (path.includes('/api/scan/queue')) return Promise.resolve(queueSnapshot)
      if (path.includes('scanner-status')) return Promise.resolve(scannerIdle)
      return Promise.resolve({ sessions: [session] })
    })
    renderDashboard()
    await waitFor(() => expect(screen.getByText('Inception')).toBeInTheDocument())
    expect(screen.getByText(/alice/)).toBeInTheDocument()
  })

  it('shows scanner scanning status when active scans present', async () => {
    const scannerActive = {
      ...scannerIdle,
      active_scans: [{ guid: 'g1', title: 'Some Movie', progress: 0.5, status: 'scanning' }],
    }
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes('events')) return Promise.resolve(emptyEvents)
      if (path.includes('/api/scan/queue')) return Promise.resolve(queueSnapshot)
      if (path.includes('scanner-status')) return Promise.resolve(scannerActive)
      return Promise.resolve(emptySessions)
    })
    renderDashboard()
    await waitFor(() => expect(screen.getByText('Some Movie')).toBeInTheDocument())
  })

  it('shows paused badge when scanner is paused', async () => {
    const paused = { ...scannerIdle, paused: true }
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes('events')) return Promise.resolve(emptyEvents)
      if (path.includes('/api/scan/queue')) return Promise.resolve(queueSnapshot)
      if (path.includes('scanner-status')) return Promise.resolve(paused)
      return Promise.resolve(emptySessions)
    })
    renderDashboard()
    await waitFor(() => expect(screen.getByText(/Paused/)).toBeInTheDocument())
  })

  it('shows skipper health status', async () => {
    renderDashboard()
    const skipperLabel = await screen.findByText('Skipper:')
    expect(skipperLabel.parentElement).toHaveTextContent('Skipper: Active')
  })

  it('backs off dashboard polling while idle', async () => {
    vi.useFakeTimers()
    renderDashboard()
    await act(async () => {
      await Promise.resolve()
    })
    const callsBefore = mockApi.get.mock.calls.length
    expect(callsBefore).toBeGreaterThan(0)

    await act(async () => {
      vi.advanceTimersByTime(5000)
      await Promise.resolve()
    })
    expect(mockApi.get.mock.calls.length).toBe(callsBefore)

    await act(async () => {
      vi.advanceTimersByTime(25000)
      await Promise.resolve()
    })
    expect(mockApi.get.mock.calls.length).toBeGreaterThan(callsBefore)
  })

  it('aborts in-flight requests when component unmounts', async () => {
    const abortSpy = vi.spyOn(AbortController.prototype, 'abort')
    const { unmount } = renderDashboard()
    await waitFor(() => expect(mockApi.get).toHaveBeenCalled())
    const abortsBeforeUnmount = abortSpy.mock.calls.length
    unmount()
    expect(abortSpy.mock.calls.length).toBeGreaterThan(abortsBeforeUnmount)
    abortSpy.mockRestore()
  })
})
