export interface SycoRuntimeConfig {
  apiBase: string
  runtimeMode: string
  localRole: string
  supabaseUrl: string
  supabaseAnonKey: string
}

interface SycoRuntimePin extends Partial<SycoRuntimeConfig> {
  enabled?: boolean
}

interface SycoAppConfig {
  sycoRuntime?: SycoRuntimePin | null
}

export function useSycoRuntime(): SycoRuntimeConfig {
  const runtimeConfig = useRuntimeConfig()
  const appConfig = useAppConfig() as SycoAppConfig
  const candidate = appConfig.sycoRuntime
  const pinned = candidate?.enabled === true ? candidate : null

  return {
    apiBase: String(pinned?.apiBase ?? runtimeConfig.public.apiBase ?? 'http://localhost:8000'),
    runtimeMode: String(pinned?.runtimeMode ?? runtimeConfig.public.runtimeMode ?? 'local'),
    localRole: String(pinned?.localRole ?? runtimeConfig.public.localRole ?? 'viewer'),
    supabaseUrl: String(pinned?.supabaseUrl ?? runtimeConfig.public.supabaseUrl ?? ''),
    supabaseAnonKey: String(pinned?.supabaseAnonKey ?? runtimeConfig.public.supabaseAnonKey ?? '')
  }
}
