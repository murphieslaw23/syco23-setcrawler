import { computed, ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

type SessionLike = { access_token: string; user: { id: string; email?: string } }

const tick = async () => { await Promise.resolve(); await Promise.resolve(); await Promise.resolve() }

describe('useAuth', () => {
  let stores: Map<string, ReturnType<typeof ref>>
  let fetchMock: ReturnType<typeof vi.fn>
  let authCallback: ((event: string, session: SessionLike | null) => void) | undefined
  let getSession: ReturnType<typeof vi.fn>

  beforeEach(() => {
    vi.resetModules()
    stores = new Map()
    fetchMock = vi.fn()
    getSession = vi.fn()
    authCallback = undefined
    vi.stubGlobal('computed', computed)
    vi.stubGlobal('useState', (key: string, initial: () => unknown) => {
      if (!stores.has(key)) stores.set(key, ref(initial()))
      return stores.get(key)
    })
    vi.stubGlobal('useRuntimeConfig', () => ({ public: { runtimeMode: 'production', localRole: 'admin', apiBase: 'http://api.test' } }))
    vi.stubGlobal('$fetch', fetchMock)
    vi.stubGlobal('useNuxtApp', () => ({
      $supabase: {
        auth: {
          getSession,
          onAuthStateChange: (callback: typeof authCallback) => {
            authCallback = callback
            return { data: { subscription: { unsubscribe: vi.fn() } } }
          },
          signOut: vi.fn().mockResolvedValue({ error: null })
        }
      }
    }))
  })

  afterEach(() => vi.unstubAllGlobals())

  it('uses /auth/me as the authoritative production role after the session is ready', async () => {
    getSession.mockResolvedValue({ data: { session: { access_token: 'token-a', user: { id: 'session-a', email: 'a@example.com' } } } })
    fetchMock.mockResolvedValue({ user_id: 'api-a', role: 'editor' })
    const { useAuth } = await import('../../composables/useAuth')
    const auth = useAuth()
    await tick()
    expect(fetchMock).toHaveBeenCalledWith('/auth/me', expect.objectContaining({ headers: { Authorization: 'Bearer token-a' } }))
    expect(auth.user.value?.id).toBe('api-a')
    expect(auth.role.value).toBe('editor')
    expect(auth.ready.value).toBe(true)
  })

  it('fences a stale identity request after sign-out', async () => {
    let resolveIdentity: ((identity: { user_id: string; role: string }) => void) | undefined
    getSession.mockResolvedValue({ data: { session: { access_token: 'token-a', user: { id: 'session-a' } } } })
    fetchMock.mockImplementation(() => new Promise((resolve) => { resolveIdentity = resolve }))
    const { useAuth } = await import('../../composables/useAuth')
    const auth = useAuth()
    await tick()
    await auth.signOut()
    resolveIdentity?.({ user_id: 'api-a', role: 'admin' })
    await tick()
    expect(auth.user.value).toBeNull()
    expect(auth.role.value).toBe('viewer')
  })

  it('keeps the newest session when identity responses complete out of order', async () => {
    const resolvers: Array<(identity: { user_id: string; role: string }) => void> = []
    getSession.mockResolvedValue({ data: { session: { access_token: 'token-a', user: { id: 'session-a' } } } })
    fetchMock.mockImplementation(() => new Promise((resolve) => resolvers.push(resolve)))
    const { useAuth } = await import('../../composables/useAuth')
    const auth = useAuth()
    await tick()
    authCallback?.('SIGNED_IN', { access_token: 'token-b', user: { id: 'session-b' } })
    await tick()
    resolvers[1]?.({ user_id: 'api-b', role: 'admin' })
    await tick()
    resolvers[0]?.({ user_id: 'api-a', role: 'editor' })
    await tick()
    expect(auth.user.value?.id).toBe('api-b')
    expect(auth.role.value).toBe('admin')
  })

  it('single-flights the same token from subscription and initial session lookup', async () => {
    let resolveIdentity: ((identity: { user_id: string; role: string }) => void) | undefined
    const session = { access_token: 'token-a', user: { id: 'session-a', email: 'a@example.com' } }
    getSession.mockResolvedValue({ data: { session } })
    fetchMock.mockImplementation(() => new Promise((resolve) => { resolveIdentity = resolve }))
    const { useAuth } = await import('../../composables/useAuth')
    const auth = useAuth()
    authCallback?.('INITIAL_SESSION', session)
    await tick()
    expect(fetchMock).toHaveBeenCalledTimes(1)
    resolveIdentity?.({ user_id: 'api-a', role: 'editor' })
    await tick()
    expect(auth.user.value?.id).toBe('api-a')
    expect(auth.role.value).toBe('editor')
    expect(auth.ready.value).toBe(true)
  })

  it('settles ready and permits a retry after initial session lookup fails', async () => {
    getSession.mockRejectedValueOnce(new Error('session unavailable')).mockResolvedValue({ data: { session: null } })
    const { useAuth } = await import('../../composables/useAuth')
    const auth = useAuth()
    await tick()
    expect(auth.ready.value).toBe(true)
    await auth.initialize()
    expect(getSession).toHaveBeenCalledTimes(2)
    expect(auth.role.value).toBe('viewer')
  })
})
