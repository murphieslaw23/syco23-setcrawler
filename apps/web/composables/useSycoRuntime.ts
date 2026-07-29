export interface SycoRuntimeConfig {
  apiBase: string
  runtimeMode: string
  localRole: string
  supabaseUrl: string
  supabaseAnonKey: string
}

interface SycoAppConfig {
  sycoRuntime?: Partial<SycoRuntimeConfig> | null
}

export function useSycoRuntime(): SycoRuntimeConfig {
  const runtimeConfig = useRuntimeConfig()
  const appConfig = useAppConfig() as SycoAppConfig
  const pinned = appConfig.sycoRuntime

  return {
    apiBase: String(pinned?.apiBase ?? runtimeConfig.public.apiBase ?? 'http://localhost:8000'),
    runtimeMode: String(pinned?.runtimeMode ?? runtimeConfig.public.runtimeMode ?? 'local'),
    localRole: String(pinned?.localRole ?? runtimeConfig.public.localRole ?? 'viewer'),
    supabaseUrl: String(pinned?.supabaseUrl ?? runtimeConfig.public.supabaseUrl ?? ''),
    supabaseAnonKey: String(pinned?.supabaseAnonKey ?? runtimeConfig.public.supabaseAnonKey ?? '')
  }
}
