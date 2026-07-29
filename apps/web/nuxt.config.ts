const isVercelRuntime = process.env.VERCEL === '1'

const productionRuntime = {
  apiBase: 'https://api.syco23.org',
  runtimeMode: 'production',
  localRole: 'viewer',
  supabaseUrl: 'https://smoevguhtsclfcmjwwhq.supabase.co',
  supabasePublishableKey: 'sb_publishable_hZXd8eJ4dGRwfHSW8u0Xog_JFhFD7s0'
}

const localRuntime = {
  apiBase: process.env.NUXT_PUBLIC_API_BASE || 'http://localhost:8000',
  runtimeMode: process.env.NUXT_PUBLIC_RUNTIME_MODE || 'local',
  localRole: process.env.NUXT_PUBLIC_LOCAL_ROLE || 'admin',
  supabaseUrl: process.env.NUXT_PUBLIC_SUPABASE_URL || '',
  supabasePublishableKey: process.env.NUXT_PUBLIC_SUPABASE_ANON_KEY || ''
}

// Vercel previously retained stale project-level variables for a different
// Supabase project and localhost API. Keep the approved public production
// coordinates authoritative until those platform variables are removed.
const runtime = isVercelRuntime ? productionRuntime : localRuntime

export default defineNuxtConfig({
  compatibilityDate: '2026-07-28',
  devtools: { enabled: false },
  css: ['~/assets/app.css'],
  runtimeConfig: {
    public: {
      apiBase: runtime.apiBase,
      runtimeMode: runtime.runtimeMode,
      localRole: runtime.localRole,
      supabaseUrl: runtime.supabaseUrl,
      supabaseAnonKey: runtime.supabasePublishableKey
    }
  },
  app: {
    head: {
      title: 'SYCO23 Setcrawler',
      meta: [
        { name: 'description', content: 'SYCO23 liveset discovery and editorial review.' },
        { name: 'theme-color', content: '#171311' }
      ]
    }
  },
  typescript: {
    strict: true,
    typeCheck: false
  }
})
