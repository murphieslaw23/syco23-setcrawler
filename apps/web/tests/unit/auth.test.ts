import { describe, expect, it } from 'vitest'

import { canChangeSetStatus, canManageProviders, capabilities, headersForRuntime, roleFromIdentity } from '../../utils/auth'

describe('role capabilities', () => {
  it('keeps viewers read-only', () => {
    expect(capabilities('viewer')).toEqual({ edit: false, admin: false })
  })

  it('permits editors to curate but not operate providers', () => {
    expect(capabilities('editor')).toEqual({ edit: true, admin: false })
  })

  it('permits admins to curate and operate providers', () => {
    expect(capabilities('admin')).toEqual({ edit: true, admin: true })
  })

  it('uses a local role header for every non-production mode', () => {
    expect(headersForRuntime('local', 'editor', 'ignored-token')).toEqual({ 'X-Local-Role': 'editor' })
    expect(headersForRuntime('fixture', 'viewer', null)).toEqual({ 'X-Local-Role': 'viewer' })
  })

  it('uses only a bearer token in production', () => {
    expect(headersForRuntime('production', 'admin', 'session-token')).toEqual({ Authorization: 'Bearer session-token' })
    expect(headersForRuntime('production', 'admin', null)).toEqual({})
  })

  it('accepts roles only from the API identity contract', () => {
    expect(roleFromIdentity({ user_id: '123', role: 'editor' })).toBe('editor')
    expect(roleFromIdentity({ user_id: '123', role: 'not-a-role' })).toBe('viewer')
  })

  it('keeps publishing admin-only while editors can curate', () => {
    expect(canChangeSetStatus('editor', 'accepted')).toBe(true)
    expect(canChangeSetStatus('editor', 'published')).toBe(false)
    expect(canChangeSetStatus('admin', 'published')).toBe(true)
    expect(canManageProviders('editor')).toBe(false)
    expect(canManageProviders('admin')).toBe(true)
  })
})
