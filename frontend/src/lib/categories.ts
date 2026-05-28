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
    key: 'sex_nudity_immodesty',
    label: 'Nudity & Immodesty',
    description: 'VidAngel nudity and immodesty filters.',
    default_threshold: 0.4,
  },
  {
    key: 'sex_any',
    label: 'Sex',
    description: 'VidAngel sex-content filters.',
    default_threshold: 0.5,
  },
  {
    key: 'kissing',
    label: 'Kissing',
    description: 'VidAngel kissing filters.',
    default_threshold: 0.5,
  },
  {
    key: 'violence_blood_gore',
    label: 'Violence',
    description: 'VidAngel violence and gore filters.',
    default_threshold: 0.5,
  },
  {
    key: 'human_functions',
    label: 'Medical & Body Process',
    description: 'VidAngel medical and body-process filters.',
    default_threshold: 0.5,
  },
  {
    key: 'alcohol_or_drug_use',
    label: 'Drugs & Alcohol',
    description: 'VidAngel drugs and alcohol filters.',
    default_threshold: 0.5,
  },
  {
    key: 'language_blasphemy',
    label: 'Blasphemy',
    description: 'VidAngel blasphemy language filters.',
    default_threshold: 0.5,
  },
  {
    key: 'language_language_childish',
    label: 'Childish Language',
    description: 'VidAngel childish language filters.',
    default_threshold: 0.5,
  },
  {
    key: 'language_language_racial',
    label: 'Racial & Bigoted Slurs',
    description: 'VidAngel racial and bigoted slur filters.',
    default_threshold: 0.5,
  },
  {
    key: 'language_language_sexual',
    label: 'Sexual Reference',
    description: 'VidAngel sexual-reference language filters.',
    default_threshold: 0.5,
  },
  {
    key: 'language_profanity',
    label: 'Profanity',
    description: 'VidAngel profanity language filters.',
    default_threshold: 0.5,
  },
  {
    key: 'language_profanity_captions',
    label: 'Captions with Profanity',
    description: 'VidAngel profanity-caption filters.',
    default_threshold: 0.5,
  },
  {
    key: 'credits',
    label: 'Credits & Extras',
    description: 'VidAngel credits and extras filters.',
    default_threshold: 0.5,
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
