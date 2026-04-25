import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, waitFor, fireEvent, act, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import UsersPage from '../../pages/Users'

vi.mock('../../api/client', () => ({
  api: {
    get: vi.fn(),
    put: vi.fn(),
  },
}))

import { api } from '../../api/client'
const mockApi = api as {
  get: ReturnType<typeof vi.fn>
  put: ReturnType<typeof vi.fn>
}

const sexualContentCategory = {
  key: 'sexual_content',
  label: 'Sexual Content',
  description: 'Skip detected sexual activity or suggestive intimate scenes.',
  default_threshold: 0.55,
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
}

function renderUsers() {
  return render(
    <MemoryRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
      <UsersPage />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  mockApi.get.mockImplementation((path: string) => {
    if (path.includes('/api/settings/categories')) {
      return Promise.resolve({
        categories: [
          { key: 'nudity', label: 'Nudity', description: 'Skip detected nudity scenes.', default_threshold: 0.4 },
          sexualContentCategory,
          { key: 'profanity', label: 'Profanity', description: 'Skip subtitle or transcript profanity matches.', default_threshold: 0.5 },
          { key: 'violence', label: 'Violence', description: 'Skip detected violence, blood, or weapon scenes.', default_threshold: 0.55 },
          { key: 'drugs', label: 'Drugs', description: 'Skip detected drug use or paraphernalia scenes.', default_threshold: 0.55 },
        ],
      })
    }
    return Promise.resolve({
      users: [
        {
          username: 'alice',
          thumb: '',
          enabled: true,
          categories: {
            nudity: { enabled: true, threshold: 0.4 },
            sexual_content: {
              enabled: true,
              threshold: 0.6,
              labels: {
                explicit_sex: { enabled: true, threshold: null },
                brief_kiss: { enabled: false, threshold: null },
              },
            },
            profanity: { enabled: false, threshold: 0.5 },
          },
        },
      ],
    })
  })
  mockApi.put.mockResolvedValue({ ok: true })
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('Users', () => {
  it('renders all filter categories even when the API omits some preferences', async () => {
    renderUsers()
    await waitFor(() => expect(screen.getByText('Profiles')).toBeInTheDocument())
    expect(screen.getByText('Nudity')).toBeInTheDocument()
    expect(screen.getByText('Sexual Content')).toBeInTheDocument()
    expect(screen.getByText('Profanity')).toBeInTheDocument()
    expect(screen.getByText('Violence')).toBeInTheDocument()
    expect(screen.getByText('Drugs')).toBeInTheDocument()
  })

  it('saves a category preference toggle', async () => {
    renderUsers()
    await waitFor(() => expect(screen.getByText('Nudity')).toBeInTheDocument())

    const profanityLabel = screen.getByText('Profanity')
    const profanityCard = profanityLabel.closest('div.rounded-lg')
    expect(profanityCard).toBeTruthy()
    const profanityToggle = within(profanityCard as HTMLElement).getByRole('button')

    await act(async () => {
      fireEvent.click(profanityToggle)
    })

    await waitFor(() => expect(mockApi.put).toHaveBeenCalled())
    expect(mockApi.put).toHaveBeenCalledWith(
      '/api/users/alice/categories/profanity',
      expect.objectContaining({ enabled: true }),
    )
  })

  it('does not crash when categories are entirely missing', async () => {
    mockApi.get.mockImplementation((path: string) => {
      if (path.includes('/api/settings/categories')) {
        return Promise.resolve({
          categories: [
            { key: 'nudity', label: 'Nudity', description: 'Skip detected nudity scenes.', default_threshold: 0.4 },
            sexualContentCategory,
            { key: 'profanity', label: 'Profanity', description: 'Skip subtitle or transcript profanity matches.', default_threshold: 0.5 },
            { key: 'violence', label: 'Violence', description: 'Skip detected violence, blood, or weapon scenes.', default_threshold: 0.55 },
            { key: 'drugs', label: 'Drugs', description: 'Skip detected drug use or paraphernalia scenes.', default_threshold: 0.55 },
          ],
        })
      }
      return Promise.resolve({
        users: [
          {
            username: 'bob',
            thumb: '',
            enabled: true,
            categories: {},
          },
        ],
      })
    })

    renderUsers()
    await waitFor(() => expect(screen.getByText('bob')).toBeInTheDocument())
    expect(screen.getByText('Sexual Content')).toBeInTheDocument()
    expect(screen.getByText('Violence')).toBeInTheDocument()
    expect(screen.getByText('Drugs')).toBeInTheDocument()
  })

  it('saves a granular label preference toggle', async () => {
    renderUsers()
    await waitFor(() => expect(screen.getByText('Brief Kiss')).toBeInTheDocument())

    const label = screen.getByText('Brief Kiss').closest('label')
    expect(label).toBeTruthy()
    const checkbox = within(label as HTMLElement).getByRole('checkbox')

    await act(async () => {
      fireEvent.click(checkbox)
    })

    await waitFor(() => expect(mockApi.put).toHaveBeenCalledWith(
      '/api/users/alice/categories/sexual_content/labels/brief_kiss',
      { enabled: true, threshold: null },
    ))
  })
})
