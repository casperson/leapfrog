import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import RewriteMediaPage from '../../pages/RewriteMedia'

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

const candidateResponse = {
  settings: {
    selected_leaf_keys: ['fuck'],
    language_mode: 'mute',
    profile_user: 'alice',
    size_limit_percent: 120,
    output_container: 'mkv',
    output_strategy: 'sibling',
  },
  candidates: [
    {
      media_id: 'movie-1',
      title: 'Sample Movie',
      file_path: '/movies/sample.mp4',
      library_id: '1',
      library_title: 'Movies',
      media_type: 'movie',
      year: 2019,
      sidecar_path: '/movies/sample.leapfrog.json',
      source_type: 'sidecar',
      rewrite_ready: true,
      unsupported_reason: null,
      available_leaf_count: 2,
      output_path: '/movies/sample.cleaned.mkv',
    },
    {
      media_id: 'movie-2',
      title: 'Legacy Movie',
      file_path: '/movies/legacy.mp4',
      library_id: '1',
      library_title: 'Movies',
      media_type: 'movie',
      year: 2004,
      sidecar_path: '/movies/legacy.leapfrog.json',
      source_type: 'sidecar',
      rewrite_ready: false,
      unsupported_reason: 'Segment cannot be mapped to an exact VidAngel leaf key.',
      available_leaf_count: 0,
      output_path: '/movies/legacy.cleaned.mkv',
    },
  ],
}

const usersResponse = {
  users: [
    { username: 'alice', enabled: true },
    { username: 'bob', enabled: true },
  ],
}

const categoriesResponse = {
  categories: [
    {
      key: 'language_profanity',
      label: 'Profanity',
      description: 'Language filters.',
      default_threshold: 0.5,
      labels: [
        { key: 'fuck', label: 'Fu**', description: 'f-word', default_skip: true, default_detect: false },
        { key: 'shit', label: 'Sh**', description: 's-word', default_skip: true, default_detect: false },
      ],
    },
    {
      key: 'sex_nudity_immodesty',
      label: 'Nudity & Immodesty',
      description: 'Nudity filters.',
      default_threshold: 0.4,
      labels: [
        { key: 'immodesty_female', label: 'Female Immodesty', description: 'female immodesty', default_skip: true, default_detect: false },
      ],
    },
  ],
}

function renderPage() {
  return render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <RewriteMediaPage />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  mockApi.get.mockImplementation((path: string) => {
    if (path === '/api/rewrite/candidates') return Promise.resolve(candidateResponse)
    if (path === '/api/users') return Promise.resolve(usersResponse)
    if (path === '/api/settings/categories') return Promise.resolve(categoriesResponse)
    if (path === '/api/rewrite/profile-leaf-keys/alice') {
      return Promise.resolve({ selected_leaf_keys: ['fuck', 'shit'] })
    }
    return Promise.resolve({})
  })
  mockApi.post.mockImplementation((path: string) => {
    if (path === '/api/rewrite/plan') {
      return Promise.resolve({
        plans: [
          {
            media_id: 'movie-1',
            title: 'Sample Movie',
            selected_leaf_keys: ['fuck', 'shit'],
            matched_leaf_keys: ['fuck'],
            language_mode: 'mute',
            summary: {
              cut_duration_seconds: 0,
              mute_duration_seconds: 2,
              kept_duration_seconds: 100,
              output_path: '/movies/sample.cleaned.mkv',
            },
          },
        ],
      })
    }
    if (path === '/api/rewrite/jobs') return Promise.resolve({ job_id: 7, status: 'running' })
    return Promise.resolve({})
  })
  mockApi.put.mockResolvedValue(candidateResponse.settings)
})

afterEach(() => {
  vi.clearAllMocks()
  vi.restoreAllMocks()
})

describe('RewriteMedia', () => {
  it('shows rewrite-ready titles and unsupported reasons', async () => {
    renderPage()

    await waitFor(() => expect(screen.getByText('Rewrite Media')).toBeInTheDocument())
    expect(screen.getByText('Sample Movie')).toBeInTheDocument()
    expect(screen.getByText('Unsupported titles')).toBeInTheDocument()
    expect(screen.getByText(/Segment cannot be mapped/)).toBeInTheDocument()
  })

  it('loads leaf keys from a saved user profile', async () => {
    renderPage()

    await waitFor(() => expect(screen.getByText('Load profile filters')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Load profile filters'))

    await waitFor(() =>
      expect(mockApi.get).toHaveBeenCalledWith('/api/rewrite/profile-leaf-keys/alice')
    )
  })

  it('posts selected titles, leaf keys, and language overrides in preview requests', async () => {
    renderPage()

    await waitFor(() => expect(screen.getByText('Sample Movie')).toBeInTheDocument())
    fireEvent.click(screen.getAllByRole('checkbox')[0])
    fireEvent.click(screen.getByText('Load profile filters'))
    fireEvent.click(screen.getAllByText('Cut')[0])
    await waitFor(() => expect(screen.getByText('Preview rewrite')).toBeInTheDocument())
    fireEvent.click(screen.getByText('Preview rewrite'))

    await waitFor(() =>
      expect(mockApi.post).toHaveBeenCalledWith('/api/rewrite/plan', {
        media_ids: ['movie-1'],
        selected_leaf_keys: ['fuck', 'shit'],
        profile_user: 'alice',
        language_mode: 'mute',
        per_media_language_mode: { 'movie-1': 'cut' },
      })
    )
  })

  it('shows an error message when starting a rewrite job fails', async () => {
    mockApi.post.mockImplementation((path: string) => {
      if (path === '/api/rewrite/jobs') return Promise.reject(new Error('Job start failed'))
      if (path === '/api/rewrite/plan') {
        return Promise.resolve({
          plans: [],
        })
      }
      return Promise.resolve({})
    })

    renderPage()

    await waitFor(() => expect(screen.getByText('Sample Movie')).toBeInTheDocument())
    fireEvent.click(screen.getAllByRole('checkbox')[0])
    fireEvent.click(screen.getByText('Start rewrite job'))

    await waitFor(() => expect(screen.getByText('Job start failed')).toBeInTheDocument())
  })
})
