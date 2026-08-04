import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, within, act } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import VidAngelExportPage from '../../pages/VidAngelExport'

vi.mock('../../api/client', () => ({
  api: {
    get: vi.fn(),
    postText: vi.fn(),
  },
}))

import { api } from '../../api/client'
const mockApi = api as {
  get: ReturnType<typeof vi.fn>
  postText: ReturnType<typeof vi.fn>
}

const catalogResponse = {
  available: true,
  title_count: 1,
  titles: [
    {
      media_id: 'movie-1',
      media_type: 'movie',
      title: 'Angel Has Fallen',
      slug: 'angel-has-fallen',
      event_count: 4,
      category_count: 2,
      year: 2019,
      rating: 'R',
    },
  ],
}

const filtersResponse = {
  title: catalogResponse.titles[0],
  leaf_count: 3,
  event_count: 4,
  categories: [
    {
      key: 'language_profanity',
      label: 'Profanity',
      description: 'Profanity language filters.',
      filters: [
        {
          leaf_key: 'fuck',
          label: 'Fu**',
          description: 'Example f-word usage.',
          category: 'language_profanity',
          category_label: 'Profanity',
          event_count: 2,
          tag_type: 'audio',
          events: [
            {
              event_id: 'event-1',
              start_ms: 1000,
              end_ms: 1200,
              description: 'First f-word event',
            },
            {
              event_id: 'event-2',
              start_ms: 3000,
              end_ms: 3200,
              description: 'Second f-word event',
            },
          ],
        },
        {
          leaf_key: 'shit',
          label: 'Sh**',
          description: 'Example s-word usage.',
          category: 'language_profanity',
          category_label: 'Profanity',
          event_count: 1,
          tag_type: 'audio',
          events: [
            {
              event_id: 'event-3',
              start_ms: 5000,
              end_ms: 5200,
              description: 'One s-word event',
            },
          ],
        },
      ],
    },
    {
      key: 'sexual_content',
      label: 'Sexual Content',
      description: 'Sexual-content filters.',
      filters: [
        {
          leaf_key: 'immodesty_female',
          label: 'Female Immodesty',
          description: 'Example immodesty filter.',
          category: 'sexual_content',
          category_label: 'Nudity & Immodesty',
          event_count: 1,
          tag_type: 'audiovisual',
          events: [
            {
              event_id: 'event-4',
              start_ms: 6000,
              end_ms: 9000,
              description: 'Visible immodesty event',
            },
          ],
        },
      ],
    },
  ],
}

function renderPage() {
  return render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <VidAngelExportPage />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  mockApi.get.mockImplementation((path: string) => {
    if (path === '/api/vidangel/export/catalog') {
      return Promise.resolve(catalogResponse)
    }
    if (path.includes('/api/vidangel/export/titles/movie-1/filters')) {
      return Promise.resolve(filtersResponse)
    }
    return Promise.resolve({})
  })
  mockApi.postText.mockResolvedValue('{"SceneFileTypeId":1,"SkipScenes":[]}')
  vi.stubGlobal('URL', {
    createObjectURL: vi.fn(() => 'blob:vidangel'),
    revokeObjectURL: vi.fn(),
  } as any)
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {})
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

describe('VidAngelExport', () => {
  it('loads the title list and filter groups for the selected title', async () => {
    renderPage()

    await waitFor(() => expect(screen.getByText('VidAngel Export')).toBeInTheDocument())
    await waitFor(() => expect(screen.getByRole('heading', { name: 'Profanity' })).toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'Angel Has Fallen (2019)' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Sexual Content' })).toBeInTheDocument()
    expect(screen.getByText('Nudity & Immodesty')).toBeInTheDocument()
    expect(screen.getByText('4 / 4 selected')).toBeInTheDocument()
    expect(screen.getByText('First f-word event')).toBeInTheDocument()
    expect(screen.getByText('0:00:01 → 0:00:01.2')).toBeInTheDocument()
  })

  it('searches library titles and separates movies from TV episodes', async () => {
    const mixedCatalog = {
      ...catalogResponse,
      title_count: 4,
      titles: [
        ...catalogResponse.titles,
        {
          ...catalogResponse.titles[0],
          media_id: 'movie-2',
          title: 'Another Movie',
          slug: 'another-movie',
          year: 2020,
        },
        {
          ...catalogResponse.titles[0],
          media_id: 'episode-1',
          media_type: 'episode',
          title: 'Pilot',
          show_title: 'Sample Show',
          season_number: 1,
          episode_number: 1,
        },
        {
          ...catalogResponse.titles[0],
          media_id: 'episode-2',
          media_type: 'episode',
          title: 'Finale',
          show_title: 'Sample Show',
          season_number: 1,
          episode_number: 8,
        },
      ],
    }
    mockApi.get.mockImplementation((path: string) => {
      if (path === '/api/vidangel/export/catalog') return Promise.resolve(mixedCatalog)
      return Promise.resolve(filtersResponse)
    })
    renderPage()

    await waitFor(() => expect(screen.getByRole('button', { name: 'Movies (2)' })).toBeInTheDocument())
    expect(screen.queryByRole('button', { name: /Sample Show/ })).not.toBeInTheDocument()

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'TV (2)' }))
    })
    const titleSearch = screen.getByPlaceholderText('Search TV episodes')
    await act(async () => {
      fireEvent.change(titleSearch, { target: { value: 'pilot' } })
    })

    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Sample Show · S01E01 · Pilot' })).toBeInTheDocument(),
    )
    expect(screen.getAllByText('Sample Show · S01E01 · Pilot')).toHaveLength(2)
    expect(screen.queryByRole('button', { name: /Finale/ })).not.toBeInTheDocument()
  })

  it('keeps an explicitly selected empty media tab active while filters are loading', async () => {
    let resolveFilters!: (value: typeof filtersResponse) => void
    const pendingFilters = new Promise<typeof filtersResponse>(resolve => {
      resolveFilters = resolve
    })
    mockApi.get.mockImplementation((path: string) => {
      if (path === '/api/vidangel/export/catalog') return Promise.resolve(catalogResponse)
      return pendingFilters
    })
    renderPage()
    await waitFor(() => expect(screen.getByRole('button', { name: 'TV (0)' })).toBeInTheDocument())
    await waitFor(() => expect(screen.getByText('Loading filters...')).toBeInTheDocument())

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'TV (0)' }))
    })

    expect(screen.getByRole('button', { name: 'TV (0)' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByText('No matching TV episodes in your library.')).toBeInTheDocument()
    expect(screen.getByText('Choose a title to begin')).toBeInTheDocument()
    expect(screen.getByText('0 / 0 selected')).toBeInTheDocument()
    expect(screen.queryByText('Loading filters...')).not.toBeInTheDocument()

    await act(async () => {
      resolveFilters(filtersResponse)
      await pendingFilters
    })
  })

  it('updates the selected count when one exact event is toggled', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('First f-word event')).toBeInTheDocument())

    const label = screen.getByText('First f-word event').closest('label')
    expect(label).toBeTruthy()
    const checkbox = within(label as HTMLElement).getByRole('checkbox')

    await act(async () => {
      fireEvent.click(checkbox)
    })

    expect(screen.getByText('3 / 4 selected')).toBeInTheDocument()
  })

  it('uses complete groups when toggling during an event search', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('First f-word event')).toBeInTheDocument())

    fireEvent.change(
      screen.getByPlaceholderText('Search filters, event descriptions, or timestamps'),
      { target: { value: 'First f-word event' } },
    )

    expect(screen.queryByText('Second f-word event')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Clear category' }))
    expect(screen.getByText('1 / 4 selected')).toBeInTheDocument()

    const filterLabel = screen.getByText('Fu**').closest('label')
    expect(filterLabel).toBeTruthy()
    fireEvent.click(within(filterLabel as HTMLElement).getByRole('checkbox'))
    expect(screen.getByText('3 / 4 selected')).toBeInTheDocument()
  })

  it('collapses and expands filter categories without changing selection', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('First f-word event')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: 'Collapse Profanity' }))

    expect(screen.queryByText('First f-word event')).not.toBeInTheDocument()
    expect(screen.getByText('4 / 4 selected')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Expand Profanity' })).toHaveAttribute('aria-expanded', 'false')

    fireEvent.click(screen.getByRole('button', { name: 'Expand Profanity' }))

    expect(screen.getByText('First f-word event')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Collapse Profanity' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('collapses and expands individual leaf filters without changing selection', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('First f-word event')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: 'Collapse Fu**' }))

    expect(screen.queryByText('First f-word event')).not.toBeInTheDocument()
    expect(screen.queryByText('Second f-word event')).not.toBeInTheDocument()
    expect(screen.getByText('One s-word event')).toBeInTheDocument()
    expect(screen.getByText('4 / 4 selected')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Expand Fu**' })).toHaveAttribute('aria-expanded', 'false')

    fireEvent.click(screen.getByRole('button', { name: 'Expand Fu**' }))

    expect(screen.getByText('First f-word event')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Collapse Fu**' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('collapses and expands every leaf filter in a category without changing selection', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('First f-word event')).toBeInTheDocument())

    fireEvent.click(screen.getByRole('button', { name: 'Collapse all filters in Profanity' }))

    expect(screen.queryByText('First f-word event')).not.toBeInTheDocument()
    expect(screen.queryByText('One s-word event')).not.toBeInTheDocument()
    expect(screen.getByText('4 / 4 selected')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Expand all filters in Profanity' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Expand all filters in Profanity' }))

    expect(screen.getByText('First f-word event')).toBeInTheDocument()
    expect(screen.getByText('One s-word event')).toBeInTheDocument()
  })

  it('downloads a filtered skp using the selected event ids', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('Generate `.skp`')).toBeInTheDocument())

    fireEvent.click(screen.getByText('Generate `.skp`'))

    await waitFor(() =>
      expect(mockApi.postText).toHaveBeenCalledWith(
        '/api/vidangel/export/titles/movie-1/skp',
        { selected_event_ids: ['event-1', 'event-2', 'event-3', 'event-4'] },
      ),
    )
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalled()
    expect(URL.createObjectURL).toHaveBeenCalled()
    const downloadedBlob = vi.mocked(URL.createObjectURL).mock.calls[0][0] as Blob
    expect(downloadedBlob.type).toBe('application/json;charset=utf-8')
  })

  it('shows a guided message when the VidAngel export data is not ready', async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path === '/api/vidangel/export/catalog') {
        return Promise.resolve(catalogResponse)
      }
      if (path.includes('/api/vidangel/export/titles/movie-1/filters')) {
        return Promise.reject(new Error('503 {"detail":"VidAngel raw filter events export is missing."}'))
      }
      return Promise.resolve({})
    })

    renderPage()

    await waitFor(() =>
      expect(screen.getByText('VidAngel export data is not ready yet. Run the VidAngel export first, then refresh this page.')).toBeInTheDocument(),
    )
  })
})
