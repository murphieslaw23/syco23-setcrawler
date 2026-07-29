const isVercelRuntime = process.env.VERCEL === '1'

const vercelDefaults = {
  apiBase: 'https://api.syco23.org',
  runtimeMode: 'production',
  supabaseUrl: 'https://smoevguhtsclfcmjwwhq.supabase.co',
  supabasePublishableKey: 'sb_publishable_hZXd8eJ4dGRwfHSW8u0Xog_JFhFD7s0'
}

export default defineNuxtConfig({
  compatibilityDate: '2026-07-28',
  devtools: { enabled: false },
  css: ['~/assets/app.css'],
  runtimeConfig: {
    public: {
      apiBase:
        process.env.NUXT_PUBLIC_API_BASE ||
        (isVercelRuntime ? vercelDefaults.apiBase : 'http://localhost:8000'),
      runtimeMode:
        process.env.NUXT_PUBLIC_RUNTIME_MODE ||
        (isVercelRuntime ? vercelDefaults.runtimeMode : 'local'),
      localRole: process.env.NUXT_PUBLIC_LOCAL_ROLE || 'admin',
      supabaseUrl:
        process.env.NUXT_PUBLIC_SUPABASE_URL ||
        (isVercelRuntime ? vercelDefaults.supabaseUrl : ''),
      supabaseAnonKey:
        process.env.NUXT_PUBLIC_SUPABASE_ANON_KEY ||
        (isVercelRuntime ? vercelDefaults.supabasePublishableKey : '')
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
