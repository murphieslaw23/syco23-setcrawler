declare module 'nuxt/schema' {
  interface AppConfigInput {
    sycoRuntime?: {
      apiBase: string
      runtimeMode: string
      localRole: string
      supabaseUrl: string
      supabaseAnonKey: string
    } | null
  }
}

export {}
