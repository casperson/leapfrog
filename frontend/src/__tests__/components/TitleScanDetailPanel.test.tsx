import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import TitleScanDetailPanel from '../../components/TitleScanDetailPanel'

vi.mock('../../api/client', () => ({
  api: {
    get: vi.fn(),
  },
}))

import { api } from '../../api/client'

const mockApi = api as {
  get: ReturnType<typeof vi.fn>
}

const categories = [
  { key: 'nudity', label: 'Nudity', description: 'Skip detected nudity scenes.', default_threshold: 0.4 },
  { key: 'sexual_content', label: 'Sexual Content', description: 'Skip detected sexual activity or suggestive intimate scenes.', default_threshold: 0.55 },
  { key: 'profanity', label: 'Profanity', description: 'Skip subtitle or transcript profanity matches.', default_threshold: 0.5 },
  { key: 'violence', label: 'Violence', description: 'Skip detected violence, blood, or weapon scenes.', default_threshold: 0.55 },
  { key: 'drugs', label: 'Drugs', description: 'Skip detected drug use or paraphernalia scenes.', default_threshold: 0.55 },
]

beforeEach(() => {
  mockApi.get.mockResolvedValue({
    media_id: 'g-contract',
    title: 'Contract Movie',
    analysis_state: 'partially_scanned',
    segment_count: 1,
    segment_counts_by_category: {
      nudity: 0,
      sexual_content: 1,
      profanity: 0,
      violence: 0,
      drugs: 0,
    },
    scan_statuses: [
      { category: 'nudity', status: 'done', source: 'nudenet', detail: 'No nudity detected', segment_count: 0, progress: 1, updated_at: '2026-04-19T12:00:00' },
      { category: 'sexual_content', status: 'done', source: 'semantic_clip', detail: 'Matched 1 segment', segment_count: 1, progress: 1, updated_at: '2026-04-19T12:01:00' },
      { category: 'profanity', status: 'pending', source: '', detail: 'Waiting for detector', segment_count: 0, progress: 0, updated_at: '2026-04-19T12:01:00' },
    ],
    stage_statuses: [
      { stage_key: 'prepare', status: 'done', source: 'scanner', detail: 'Scan job prepared.', progress: 1, created_at: '2026-04-19T12:00:00', updated_at: '2026-04-19T12:00:00' },
      { stage_key: 'sexual_content', category: 'sexual_content', status: 'done', source: 'semantic_clip', detail: 'Matched sexual-content prompts on shared frames.', progress: 1, created_at: '2026-04-19T12:01:00', updated_at: '2026-04-19T12:01:00' },
      { stage_key: 'finalize', status: 'pending', source: 'scanner', detail: 'Waiting for remaining detectors.', progress: 0.5, created_at: '2026-04-19T12:01:30', updated_at: '2026-04-19T12:01:30' },
    ],
    last_scan_time: '2026-04-19T12:01:30',
    preference_resolution_success: true,
    effective_segment_count: 1,
    queue: { state: 'queued', position: 2, priority: 0, cancel_requested: true },
    job_status: 'scanning',
  })
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('TitleScanDetailPanel', () => {
  it('renders the scan detail contract from the backend payload', async () => {
    render(
      <TitleScanDetailPanel
        plexGuid="g-contract"
        title="Contract Movie"
        categories={categories}
        reviewUser="alice"
      />,
    )

    await waitFor(() => expect(screen.getByText('Detector Timeline')).toBeInTheDocument())

    expect(screen.getByText('Queue position 2')).toBeInTheDocument()
    expect(screen.getByText('Cancel requested')).toBeInTheDocument()
    expect(screen.getByText('1 segment would currently skip for alice.')).toBeInTheDocument()
    expect(screen.getAllByText('Sexual Content').length).toBeGreaterThan(0)
    expect(screen.getByText('Matched sexual-content prompts on shared frames.')).toBeInTheDocument()
    expect(screen.getByText('Finalize')).toBeInTheDocument()
  })
})
