import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { UserCircle2, ShieldCheck, ShieldOff } from 'lucide-react'

const FILTER_CATEGORIES = ['nudity', 'profanity', 'violence', 'drugs'] as const

interface CategoryPreference {
  enabled: boolean
  threshold: number
}

interface User {
  username: string
  thumb: string
  enabled: boolean
  categories: Record<string, CategoryPreference>
}

function defaultPreference(category: string): CategoryPreference {
  return {
    enabled: category === 'nudity',
    threshold: category === 'nudity' ? 0.6 : 0.5,
  }
}

function getCategoryLabel(category: string): string {
  switch (category) {
    case 'nudity':
      return 'Nudity'
    case 'profanity':
      return 'Profanity'
    case 'violence':
      return 'Violence'
    case 'drugs':
      return 'Drugs'
    default:
      return category.charAt(0).toUpperCase() + category.slice(1)
  }
}

function getCategoryHint(category: string): string {
  switch (category) {
    case 'nudity':
      return 'Skip detected nudity scenes.'
    case 'profanity':
      return 'Skip subtitle or transcript profanity matches.'
    case 'violence':
      return 'Reserved for a future detector.'
    case 'drugs':
      return 'Reserved for a future detector.'
    default:
      return `Skip ${category} segments for this profile.`
  }
}

function normalizeCategories(categories: Record<string, CategoryPreference> | undefined): Record<string, CategoryPreference> {
  const merged: Record<string, CategoryPreference> = {}
  for (const category of FILTER_CATEGORIES) {
    merged[category] = {
      ...defaultPreference(category),
      ...(categories?.[category] ?? {}),
    }
  }
  return merged
}

function normalizeUser(user: User): User {
  return {
    ...user,
    categories: normalizeCategories(user.categories),
  }
}

export default function UsersPage() {
  const [users, setUsers] = useState<User[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState<Record<string, boolean>>({})

  useEffect(() => {
    api.get<{ users: User[] }>('/api/users').then(data => {
      setUsers(data.users.map(normalizeUser))
      setLoading(false)
    })
  }, [])

  const setSavingState = (key: string, value: boolean) => {
    setSaving(current => ({ ...current, [key]: value }))
  }

  const toggleUser = async (username: string, enabled: boolean) => {
    const savingKey = `user:${username}`
    setSavingState(savingKey, true)
    try {
      await api.put(`/api/users/${encodeURIComponent(username)}`, { enabled })
      setUsers(current =>
        current.map(user => (
          user.username === username ? { ...user, enabled } : user
        )),
      )
    } finally {
      setSavingState(savingKey, false)
    }
  }

  const setCategoryLocal = (username: string, category: string, next: Partial<CategoryPreference>) => {
    setUsers(current =>
      current.map(user => {
        if (user.username !== username) {
          return user
        }
        return {
          ...user,
          categories: {
            ...user.categories,
            [category]: {
              ...defaultPreference(category),
              ...user.categories[category],
              ...next,
            },
          },
        }
      }),
    )
  }

  const saveCategory = async (username: string, category: string, preference: CategoryPreference) => {
    const savingKey = `${username}:${category}`
    setSavingState(savingKey, true)
    try {
      await api.put(
        `/api/users/${encodeURIComponent(username)}/categories/${encodeURIComponent(category)}`,
        preference,
      )
    } finally {
      setSavingState(savingKey, false)
    }
  }

  return (
    <div className="max-w-4xl">
      <h1 className="text-2xl font-bold text-gray-100 mb-2">Profiles</h1>
      <p className="text-sm text-gray-500 mb-6">
        Linked Plex accounts inherit server-side filtering. Each profile can keep filtering on
        globally while choosing which categories Leapfrog should skip.
      </p>

      {loading ? (
        <div className="text-gray-500 text-sm">Loading profiles...</div>
      ) : users.length === 0 ? (
        <div className="bg-plex-card border border-plex-border rounded-xl p-8 text-center text-gray-500 text-sm">
          No users found. Make sure Plex is configured in Settings.
        </div>
      ) : (
        <div className="space-y-4">
          {users.map(user => (
            <div
              key={user.username}
              className="bg-plex-card border border-plex-border rounded-xl p-5 space-y-4"
            >
              <div className="flex items-center gap-4">
                {user.thumb ? (
                  <img
                    src={user.thumb}
                    alt={user.username}
                    className="w-10 h-10 rounded-full bg-plex-border object-cover flex-shrink-0"
                    onError={event => { (event.target as HTMLImageElement).style.display = 'none' }}
                  />
                ) : (
                  <div className="w-10 h-10 rounded-full bg-plex-border flex items-center justify-center flex-shrink-0">
                    <UserCircle2 size={22} className="text-gray-500" />
                  </div>
                )}

                <div className="flex-1 min-w-0">
                  <p className="font-medium text-gray-100">{user.username}</p>
                  <p className="text-xs text-gray-500 mt-0.5 flex items-center gap-1">
                    {user.enabled
                      ? <><ShieldCheck size={11} className="text-green-400" /> Server-side filtering active</>
                      : <><ShieldOff size={11} className="text-gray-600" /> Server-side filtering off</>
                    }
                  </p>
                </div>

                <button
                  onClick={() => toggleUser(user.username, !user.enabled)}
                  disabled={saving[`user:${user.username}`]}
                  className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none disabled:opacity-50 ${
                    user.enabled ? 'bg-plex-orange' : 'bg-plex-border'
                  }`}
                >
                  <span
                    className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${
                      user.enabled ? 'translate-x-6' : 'translate-x-1'
                    }`}
                  />
                </button>
              </div>

                <div className="grid gap-3 md:grid-cols-2">
                {FILTER_CATEGORIES.map(category => {
                  const preference = user.categories[category]
                  const savingKey = `${user.username}:${category}`
                  return (
                    <div
                      key={category}
                      className="rounded-lg border border-plex-border bg-plex-darker/70 px-4 py-3"
                    >
                      <div className="flex items-center justify-between gap-3 mb-3">
                        <div>
                          <p className="text-sm font-medium text-gray-100">{getCategoryLabel(category)}</p>
                          <p className="text-xs text-gray-500">
                            {getCategoryHint(category)}
                          </p>
                        </div>
                        <button
                          onClick={() => {
                            const next = {
                              ...preference,
                              enabled: !preference.enabled,
                            }
                            setCategoryLocal(user.username, category, next)
                            void saveCategory(user.username, category, next)
                          }}
                          disabled={saving[savingKey]}
                          className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors focus:outline-none disabled:opacity-50 ${
                            preference.enabled ? 'bg-plex-orange' : 'bg-plex-border'
                          }`}
                        >
                          <span
                            className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform ${
                              preference.enabled ? 'translate-x-6' : 'translate-x-1'
                            }`}
                          />
                        </button>
                      </div>

                      <label className="block text-xs text-gray-500 mb-1">Threshold</label>
                      <input
                        type="number"
                        min="0"
                        max="1"
                        step="0.05"
                        value={preference.threshold}
                        onChange={event => {
                          const nextValue = Number(event.target.value)
                          setCategoryLocal(user.username, category, { threshold: nextValue })
                        }}
                        onBlur={() => void saveCategory(user.username, category, preference)}
                        className="w-full px-3 py-2 bg-plex-card border border-plex-border rounded-lg text-sm text-gray-100 focus:outline-none focus:border-plex-orange/60 transition-colors"
                      />
                    </div>
                  )
                })}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
