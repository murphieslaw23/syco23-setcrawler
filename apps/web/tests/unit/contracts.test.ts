import { describe, expect, it } from 'vitest'

import { navigation } from '../../data/navigation'
import { formatDuration, formatScore, sourceLabel } from '../../utils/format'

describe('editorial UI contracts', () => {
  it('keeps the approved primary navigation order', () => {
    expect(navigation.map((item) => item.label)).toEqual([
      'Overview',
      'Review Inbox',
      'Imports',
      'Sets',
      'Artists',
      'Events',
      'Search Profiles',
      'Settings'
    ])
  })

  it('formats duration and score for compact review rows', () => {
    expect(formatDuration(5062)).toBe('01:24:22')
    expect(formatScore(0.82)).toBe('82%')
  })

  it('uses human provider labels without inventing brands', () => {
    expect(sourceLabel('youtube')).toBe('YouTube')
    expect(sourceLabel('soundcloud')).toBe('SoundCloud')
    expect(sourceLabel('freeteknomusic')).toBe('FreeTeknoMusic')
  })
})
