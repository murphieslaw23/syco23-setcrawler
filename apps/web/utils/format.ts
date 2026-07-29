import type { SetSource } from '~/types'

export function formatDuration(totalSeconds: number | null | undefined): string {
  if (!totalSeconds) return '00:00:00'
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  return [hours, minutes, seconds].map((value) => String(value).padStart(2, '0')).join(':')
}

export function formatScore(score: number): string {
  return `${Math.round(score * 100)}%`
}

export function sourceLabel(source: SetSource): string {
  return {
    youtube: 'YouTube',
    soundcloud: 'SoundCloud',
    freeteknomusic: 'FreeTeknoMusic'
  }[source]
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return 'Unknown date'
  return new Intl.DateTimeFormat('en-GB', {
    day: '2-digit',
    month: 'short',
    year: 'numeric'
  }).format(new Date(value))
}
