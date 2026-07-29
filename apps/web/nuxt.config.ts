export default defineNuxtConfig({
  compatibilityDate: '2026-07-28',
  devtools: { enabled: false },
  css: ['~/assets/app.css'],
  runtimeConfig: {
    public: {
      apiBase: process.env.NUXT_PUBLIC_API_BASE || 'http://localhost:8000',
      runtimeMode: process.env.NUXT_PUBLIC_RUNTIME_MODE || 'local',
      localRole: process.env.NUXT_PUBLIC_LOCAL_ROLE || 'admin',
      supabaseUrl: process.env.NUXT_PUBLIC_SUPABASE_URL || '',
      supabaseAnonKey: process.env.NUXT_PUBLIC_SUPABASE_ANON_KEY || ''
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
