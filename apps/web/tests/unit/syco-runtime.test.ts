import { afterEach, describe, expect, it, vi } from 'vitest'

import { useSycoRuntime } from '../../composables/useSycoRuntime'

describe('useSycoRuntime', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('prefers an enabled app pin over stale public runtime variables', () => {
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
        enabled: true,
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

  it('ignores a disabled app pin and keeps environment-driven local configuration', () => {
    vi.stubGlobal('useRuntimeConfig', () => ({
      public: {
        apiBase: 'http://127.0.0.1:9000',
        runtimeMode: 'fixture',
        localRole: 'editor',
        supabaseUrl: '',
        supabaseAnonKey: ''
      }
    }))
    vi.stubGlobal('useAppConfig', () => ({
      sycoRuntime: {
        enabled: false,
        apiBase: 'https://api.syco23.org',
        runtimeMode: 'production',
        localRole: 'viewer',
        supabaseUrl: 'https://smoevguhtsclfcmjwwhq.supabase.co',
        supabaseAnonKey: 'production-key'
      }
    }))

    expect(useSycoRuntime()).toMatchObject({
      apiBase: 'http://127.0.0.1:9000',
      runtimeMode: 'fixture',
      localRole: 'editor'
    })
  })
})
