import { describe, expect, it } from 'vitest'

import { fixtureValue, shouldLoadOperationalData } from '../../utils/runtime'

describe('runtime fixture gate', () => {
  it('returns demo data only for the exact fixture runtime', () => {
    expect(fixtureValue('fixture', ['demo'], [])).toEqual(['demo'])
    expect(fixtureValue('local', ['demo'], [])).toEqual([])
    expect(fixtureValue('production', ['demo'], [])).toEqual([])
  })

  it('defers a production hard load until the authenticated session is ready', () => {
    expect(shouldLoadOperationalData('production', false)).toBe(false)
    expect(shouldLoadOperationalData('production', true)).toBe(true)
    expect(shouldLoadOperationalData('local', false)).toBe(true)
  })
})
