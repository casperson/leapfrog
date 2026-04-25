import { useEffect, useState } from 'react'
import { api } from '../api/client'

export interface CategoryDefinition {
  key: string
  label: string
  description: string
  default_threshold: number
  labels?: {
    key: string
    label: string
    description: string
    default_skip: boolean
    default_detect: boolean
  }[]
}

export const FALLBACK_CATEGORY_DEFINITIONS: CategoryDefinition[] = [
  {
    key: 'nudity',
    label: 'Nudity',
    description: 'Skip detected nudity scenes.',
    default_threshold: 0.4,
  },
  {
    key: 'sexual_content',
    label: 'Sexual Content',
    description: 'Skip detected sexual activity or suggestive intimate scenes.',
    default_threshold: 0.55,
  },
  {
    key: 'profanity',
    label: 'Profanity',
    description: 'Skip subtitle or transcript profanity matches.',
    default_threshold: 0.5,
  },
  {
    key: 'violence',
    label: 'Violence',
    description: 'Skip detected violence, blood, or weapon scenes.',
    default_threshold: 0.55,
  },
  {
    key: 'drugs',
    label: 'Drugs',
    description: 'Skip detected drug use or paraphernalia scenes.',
    default_threshold: 0.55,
  },
]

let cachedCategoryDefinitions = FALLBACK_CATEGORY_DEFINITIONS

export function useCategoryDefinitions(): CategoryDefinition[] {
  const [categories, setCategories] = useState<CategoryDefinition[]>(cachedCategoryDefinitions)

  useEffect(() => {
    let cancelled = false

    api.get<{ categories: CategoryDefinition[] }>('/api/settings/categories')
      .then(data => {
        if (cancelled || !Array.isArray(data.categories) || data.categories.length === 0) {
          return
        }
        cachedCategoryDefinitions = data.categories
        setCategories(data.categories)
      })
      .catch(() => {})

    return () => {
      cancelled = true
    }
  }, [])

  return categories
}

export function formatCategoryKey(category: string): string {
  return category
    .split('_')
    .filter(Boolean)
    .map(part => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ')
}

export function getCategoryLabel(
  category: string,
  categories: CategoryDefinition[] = cachedCategoryDefinitions,
): string {
  return categories.find(entry => entry.key === category)?.label ?? formatCategoryKey(category)
}

export function getCategoryDescription(
  category: string,
  categories: CategoryDefinition[] = cachedCategoryDefinitions,
): string {
  return categories.find(entry => entry.key === category)?.description ?? `Skip ${formatCategoryKey(category)} segments for this profile.`
}
