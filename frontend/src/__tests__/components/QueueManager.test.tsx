import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act } from '@testing-library/react'
import QueueManager from '../../components/QueueManager'

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

const queueSnapshot = {
  jobs: [
    {
      plex_guid: 'queued-1',
      title: 'Queued Movie',
      queue_position: 1,
      queued_at: '2026-04-19T12:00:00',
      queue_updated_at: '2026-04-19T12:00:00',
      cancel_requested: false,
    },
  ],
  queue_size: 1,
  current: 'active-1',
  currents: ['active-1'],
  active_scans: [
    {
      guid: 'active-1',
      title: 'Active Movie',
      status: 'scanning',
      progress: 0.5,
      cancel_requested: false,
    },
  ],
  paused: false,
}

beforeEach(() => {
  mockApi.get.mockResolvedValue(queueSnapshot)
  mockApi.post.mockResolvedValue(queueSnapshot)
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('QueueManager', () => {
  it('wires queue move and cancel actions to explicit APIs', async () => {
    render(<QueueManager />)

    await waitFor(() => expect(screen.getByText('Queued Movie')).toBeInTheDocument())

    await act(async () => {
      fireEvent.click(screen.getByLabelText('Move Queued Movie up'))
    })
    expect(mockApi.post).toHaveBeenCalledWith('/api/scan/queue/queued-1/move-up', undefined)

    await act(async () => {
      fireEvent.click(screen.getByLabelText('Cancel Queued Movie'))
    })
    expect(mockApi.post).toHaveBeenCalledWith('/api/scan/queue/queued-1/cancel', undefined)
  })

  it('wires active cancel and batch cancel actions', async () => {
    render(<QueueManager />)

    await waitFor(() => expect(screen.getByText('Active Movie')).toBeInTheDocument())

    await act(async () => {
      fireEvent.click(screen.getByText('Cancel'))
    })
    expect(mockApi.post).toHaveBeenCalledWith('/api/scan/active/active-1/cancel', undefined)

    await act(async () => {
      fireEvent.click(screen.getByLabelText(/Select all queued/i))
    })
    await act(async () => {
      fireEvent.click(screen.getByText('Cancel selected'))
    })
    expect(mockApi.post).toHaveBeenCalledWith('/api/scan/queue/cancel-selected', { guids: ['queued-1'] })
  })
})
