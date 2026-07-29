import { afterEach, describe, expect, it, vi } from 'vitest'

import { useSycoRuntime } from '../../composables/useSycoRuntime'

describe('useSycoRuntime', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('prefers pinned app configuration over stale public runtime variables', () => {
    vi.stubGlobal('useRuntimeConfig', () => ({
      public: {
        apiBase: 'http://localhost:8000',
        runtimeMode: 'local',
        localRole: 'admin',
        supabaseUrl: 'https://stale.supabase.co',
        supabaseAnonKey: 'stale-key'
      }
    }))
    vi.stubGlobal('useAppConfig', () => ({
      sycoRuntime: {
        apiBase: 'https://api.syco23.org',
        runtimeMode: 'production',
        localRole: 'viewer',
        supabaseUrl: 'https://smoevguhtsclfcmjwwhq.supabase.co',
        supabaseAnonKey: 'production-key'
      }
    }))

    expect(useSycoRuntime()).toEqual({
      apiBase: 'https://api.syco23.org',
      runtimeMode: 'production',
      localRole: 'viewer',
      supabaseUrl: 'https://smoevguhtsclfcmjwwhq.supabase.co',
      supabaseAnonKey: 'production-key'
    })
  })

  it('keeps environment-driven local configuration when no app pin exists', () => {
    vi.stubGlobal('useRuntimeConfig', () => ({
      public: {
        apiBase: 'http://127.0.0.1:9000',
        runtimeMode: 'fixture',
        localRole: 'editor',
        supabaseUrl: '',
        supabaseAnonKey: ''
      }
    }))
    vi.stubGlobal('useAppConfig', () => ({ sycoRuntime: null }))

    expect(useSycoRuntime()).toMatchObject({
      apiBase: 'http://127.0.0.1:9000',
      runtimeMode: 'fixture',
      localRole: 'editor'
    })
  })
})
