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
      event_count: 3,
      category_count: 2,
      year: 2019,
      rating: 'R',
    },
  ],
}

const filtersResponse = {
  title: catalogResponse.titles[0],
  leaf_count: 3,
  event_count: 3,
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
        },
        {
          leaf_key: 'shit',
          label: 'Sh**',
          description: 'Example s-word usage.',
          category: 'language_profanity',
          category_label: 'Profanity',
          event_count: 1,
          tag_type: 'audio',
        },
      ],
    },
    {
      key: 'sex_nudity_immodesty',
      label: 'Nudity & Immodesty',
      description: 'Nudity filters.',
      filters: [
        {
          leaf_key: 'immodesty_female',
          label: 'Female Immodesty',
          description: 'Example immodesty filter.',
          category: 'sex_nudity_immodesty',
          category_label: 'Nudity & Immodesty',
          event_count: 1,
          tag_type: 'audiovisual',
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
  mockApi.postText.mockResolvedValue('0:00:01 --> 0:00:02\nskip\n')
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
    await waitFor(() => expect(screen.getByText('Profanity')).toBeInTheDocument())
    expect(screen.getByDisplayValue('Angel Has Fallen')).toBeInTheDocument()
    expect(screen.getByText('Nudity & Immodesty')).toBeInTheDocument()
    expect(screen.getByText('3 / 3 selected')).toBeInTheDocument()
  })

  it('updates the selected count when a filter is toggled', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('Fu**')).toBeInTheDocument())

    const label = screen.getByText('Fu**').closest('label')
    expect(label).toBeTruthy()
    const checkbox = within(label as HTMLElement).getByRole('checkbox')

    await act(async () => {
      fireEvent.click(checkbox)
    })

    expect(screen.getByText('2 / 3 selected')).toBeInTheDocument()
  })

  it('downloads a filtered skp using the selected leaf keys', async () => {
    renderPage()
    await waitFor(() => expect(screen.getByText('Generate `.skp`')).toBeInTheDocument())

    fireEvent.click(screen.getByText('Generate `.skp`'))

    await waitFor(() =>
      expect(mockApi.postText).toHaveBeenCalledWith(
        '/api/vidangel/export/titles/movie-1/skp',
        { selected_leaf_keys: ['fuck', 'shit', 'immodesty_female'] },
      ),
    )
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalled()
    expect(URL.createObjectURL).toHaveBeenCalled()
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
