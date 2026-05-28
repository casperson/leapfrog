import { useEffect, useState } from 'react'
import { api } from '../api/client'
import { UserCircle2, ShieldCheck, ShieldOff } from 'lucide-react'
import {
  CategoryDefinition,
  getCategoryDescription,
  getCategoryLabel,
  useCategoryDefinitions,
} from '../lib/categories'

interface CategoryPreference {
  enabled: boolean
  threshold: number
  labels?: Record<string, { enabled: boolean; threshold?: number | null }>
}

interface User {
  username: string
  thumb: string
  enabled: boolean
  categories: Record<string, CategoryPreference>
}

function defaultPreference(category: string): CategoryPreference {
  return {
    enabled: false,
    threshold: category === 'sex_nudity_immodesty' ? 0.4 : 0.5,
  }
}

function normalizeCategories(
  categoryDefinitions: CategoryDefinition[],
  categories: Record<string, CategoryPreference> | undefined,
): Record<string, CategoryPreference> {
  const merged: Record<string, CategoryPreference> = {}
  for (const definition of categoryDefinitions) {
    merged[definition.key] = {
      threshold: definition.default_threshold,
      enabled: defaultPreference(definition.key).enabled,
      ...(categories?.[definition.key] ?? {}),
      labels: Object.fromEntries(
        (definition.labels ?? []).map(label => {
          const storedLabel = (categories?.[definition.key]?.labels ?? {})[label.key]
          return [
            label.key,
            {
              enabled: storedLabel?.enabled ?? label.default_skip,
              threshold: storedLabel?.threshold ?? null,
            },
          ]
        }),
      ),
    }
  }
  return merged
}

function normalizeUser(user: User, categoryDefinitions: CategoryDefinition[]): User {
  return {
    ...user,
    categories: normalizeCategories(categoryDefinitions, user.categories),
  }
}

export default function UsersPage() {
  const categories = useCategoryDefinitions()
  const [users, setUsers] = useState<User[]>([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState<Record<string, boolean>>({})

  useEffect(() => {
    api.get<{ users: User[] }>('/api/users').then(data => {
      const nextUsers = Array.isArray(data.users) ? data.users : []
      setUsers(nextUsers.map(user => normalizeUser(user, categories)))
      setLoading(false)
    })
  }, [categories])

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
        const basePreference = {
          enabled: defaultPreference(category).enabled,
          threshold: categories.find(entry => entry.key === category)?.default_threshold ?? defaultPreference(category).threshold,
        }
        return {
          ...user,
          categories: {
            ...user.categories,
            [category]: {
              ...basePreference,
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

  const setLabelLocal = (username: string, category: string, label: string, enabled: boolean) => {
    setUsers(current =>
      current.map(user => {
        if (user.username !== username) return user
        const categoryPreference = user.categories[category] ?? defaultPreference(category)
        return {
          ...user,
          categories: {
            ...user.categories,
            [category]: {
              ...categoryPreference,
              labels: {
                ...(categoryPreference.labels ?? {}),
                [label]: {
                  ...(categoryPreference.labels?.[label] ?? {}),
                  enabled,
                },
              },
            },
          },
        }
      }),
    )
  }

  const saveLabel = async (username: string, category: string, label: string, enabled: boolean) => {
    const savingKey = `${username}:${category}:${label}`
    setSavingState(savingKey, true)
    try {
      await api.put(
        `/api/users/${encodeURIComponent(username)}/categories/${encodeURIComponent(category)}/labels/${encodeURIComponent(label)}`,
        { enabled, threshold: null },
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
                {categories.map(category => {
                  const preference = user.categories[category.key] ?? {
                    enabled: false,
                    threshold: category.default_threshold,
                  }
                  const savingKey = `${user.username}:${category.key}`
                  return (
                    <div
                      key={category.key}
                      className="rounded-lg border border-plex-border bg-plex-darker/70 px-4 py-3"
                    >
                      <div className="mb-3 flex items-center justify-between gap-3">
                        <div>
                          <p className="text-sm font-medium text-gray-100">{getCategoryLabel(category.key, categories)}</p>
                          <p className="text-xs text-gray-500">
                            {getCategoryDescription(category.key, categories)}
                          </p>
                        </div>
                        <button
                          onClick={() => {
                            const next = {
                              ...preference,
                              enabled: !preference.enabled,
                            }
                            setCategoryLocal(user.username, category.key, next)
                            void saveCategory(user.username, category.key, next)
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

                      <label className="mb-1 block text-xs text-gray-500">Threshold</label>
                      <input
                        type="number"
                        min="0"
                        max="1"
                        step="0.05"
                        value={preference.threshold}
                        onChange={event => {
                          const nextValue = Number(event.target.value)
                          setCategoryLocal(user.username, category.key, { threshold: nextValue })
                        }}
                        onBlur={event => {
                          const next = {
                            ...preference,
                            threshold: Number(event.target.value),
                          }
                          void saveCategory(user.username, category.key, next)
                        }}
                        className="w-full rounded-lg border border-plex-border bg-plex-card px-3 py-2 text-sm text-gray-100 transition-colors focus:border-plex-orange/60 focus:outline-none"
                      />
                      {(category.labels?.length ?? 0) > 0 && (
                        <div className="mt-3 space-y-1.5 border-t border-plex-border/70 pt-3">
                          <p className="text-xs font-medium text-gray-500">Labels to skip</p>
                          {category.labels!.map(label => {
                            const labelPreference = preference.labels?.[label.key] ?? { enabled: label.default_skip }
                            const labelSavingKey = `${user.username}:${category.key}:${label.key}`
                            return (
                              <label key={label.key} className="flex items-start gap-2 text-xs text-gray-400">
                                <input
                                  type="checkbox"
                                  checked={labelPreference.enabled}
                                  disabled={!preference.enabled || saving[labelSavingKey]}
                                  onChange={event => {
                                    const enabled = event.target.checked
                                    setLabelLocal(user.username, category.key, label.key, enabled)
                                    void saveLabel(user.username, category.key, label.key, enabled)
                                  }}
                                  className="mt-0.5 h-4 w-4 flex-shrink-0 accent-plex-orange disabled:opacity-40"
                                />
                                <span>
                                  <span className="text-gray-300">{label.label}</span>
                                  <span className="block text-gray-600">{label.description}</span>
                                </span>
                              </label>
                            )
                          })}
                        </div>
                      )}
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
