import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import LogsPage from '../../pages/Logs'

vi.mock('../../api/client', () => ({
  api: {
    get: vi.fn(),
  },
}))

import { api } from '../../api/client'

const mockApi = api as {
  get: ReturnType<typeof vi.fn>
}

class MockEventSource {
  static instances: MockEventSource[] = []

  url: string
  onopen: ((event: Event) => void) | null = null
  onmessage: ((event: MessageEvent<string>) => void) | null = null
  onerror: ((event: Event) => void) | null = null
  listeners = new Map<string, Set<(event: MessageEvent<string>) => void>>()

  constructor(url: string) {
    this.url = url
    MockEventSource.instances.push(this)
  }

  addEventListener(type: string, listener: (event: MessageEvent<string>) => void) {
    const current = this.listeners.get(type) ?? new Set()
    current.add(listener)
    this.listeners.set(type, current)
  }

  removeEventListener(type: string, listener: (event: MessageEvent<string>) => void) {
    this.listeners.get(type)?.delete(listener)
  }

  close() {}
}

beforeEach(() => {
  MockEventSource.instances = []
  mockApi.get.mockResolvedValue({
    entries: [
      {
        id: 1,
        timestamp: '2026-04-19T12:00:00',
        level: 'INFO',
        source: 'leapfrog.scanner',
        message: 'History line',
      },
    ],
  })
  vi.stubGlobal('EventSource', MockEventSource as unknown as typeof EventSource)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.clearAllMocks()
})

describe('Logs page', () => {
  it('loads history and appends SSE updates', async () => {
    render(
      <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
        <LogsPage />
      </MemoryRouter>,
    )

    await waitFor(() => expect(screen.getByText('History line')).toBeInTheDocument())
    expect(MockEventSource.instances[0]?.url).toContain('/api/logs/stream')

    await act(async () => {
      MockEventSource.instances[0]?.onopen?.(new Event('open'))
      const event = new MessageEvent('log', {
        data: JSON.stringify({
          id: 2,
          timestamp: '2026-04-19T12:00:05',
          level: 'ERROR',
          source: 'leapfrog.detectors.semantic',
          message: 'Live line',
        }),
      })
      MockEventSource.instances[0]?.listeners.get('log')?.forEach(listener => listener(event))
    })

    await waitFor(() => expect(screen.getByText('Live line')).toBeInTheDocument())
    expect(screen.getByText('Connected')).toBeInTheDocument()
  })
})
