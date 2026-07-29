import type { Session, SupabaseClient } from '@supabase/supabase-js'

import { computed } from 'vue'

import { capabilities, headersForRuntime, roleFromIdentity, type ApiIdentity, type AppRole } from '~/utils/auth'

interface AuthUser {
  id: string
  email?: string
}

interface AuthState {
  user: AuthUser | null
  role: AppRole
  ready: boolean
}

let authUnsubscribe: (() => void) | undefined
let generation = 0
let activeToken: string | null = null
let inflightToken: string | null = null
let inflightIdentity: Promise<void> | undefined

export function useAuth() {
  const runtime = useSycoRuntime()
  const clientRuntime = import.meta.client || typeof window !== 'undefined'
  const runtimeMode = runtime.runtimeMode
  const localRole = ((runtime.localRole as AppRole) || 'viewer')
  const app = useNuxtApp()
  const supabase = app.$supabase as SupabaseClient | null | undefined
  const state = useState<AuthState>('syco-auth', () => ({
    user: null,
    role: runtimeMode === 'production' ? 'viewer' : localRole,
    ready: runtimeMode !== 'production'
  }))
  const initialized = useState('syco-auth-initialized', () => false)

  const role = computed(() => state.value.role)
  const user = computed(() => state.value.user)
  const ready = computed(() => state.value.ready)
  const canEdit = computed(() => capabilities(role.value).edit)
  const isAdmin = computed(() => capabilities(role.value).admin)

  function reset() {
    generation += 1
    activeToken = null
    inflightToken = null
    inflightIdentity = undefined
    state.value = {
      user: null,
      role: runtimeMode === 'production' ? 'viewer' : localRole,
      ready: true
    }
  }

  async function resolveProductionIdentity(session: Session, requestGeneration: number) {
    const token = session.access_token
    const identity = await $fetch<ApiIdentity>('/auth/me', {
      baseURL: runtime.apiBase,
      headers: { Authorization: `Bearer ${token}` },
      timeout: 5_000
    })
    if (requestGeneration !== generation || activeToken !== token) return
    state.value = {
      user: { id: identity.user_id, email: session.user.email },
      role: roleFromIdentity(identity),
      ready: true
    }
  }

  async function applySession(session: Session | null) {
    if (runtimeMode !== 'production') {
      generation += 1
      activeToken = session?.access_token ?? null
      state.value = { user: session ? { id: session.user.id, email: session.user.email } : null, role: localRole, ready: true }
      return
    }
    if (!session) {
      reset()
      return
    }
    const token = session.access_token
    if (token === activeToken) {
      if (inflightToken === token && inflightIdentity) await inflightIdentity
      return
    }
    const requestGeneration = ++generation
    activeToken = token
    state.value = { user: null, role: 'viewer', ready: false }
    const request = resolveProductionIdentity(session, requestGeneration)
      .catch(() => {
        if (requestGeneration === generation && activeToken === token) reset()
      })
      .finally(() => {
        if (inflightToken === token) {
          inflightToken = null
          inflightIdentity = undefined
        }
        if (requestGeneration === generation && activeToken === token && !state.value.ready) state.value.ready = true
      })
    inflightToken = token
    inflightIdentity = request
    await request
  }

  function subscribe() {
    if (!supabase || authUnsubscribe) return
    const { data: subscription } = supabase.auth.onAuthStateChange((_event, session) => {
      void applySession(session)
    })
    authUnsubscribe = () => subscription.subscription.unsubscribe()
  }

  async function initialize() {
    if (initialized.value || !clientRuntime) return
    initialized.value = true
    if (!supabase) {
      reset()
      return
    }
    try {
      subscribe()
      const { data } = await supabase.auth.getSession()
      await applySession(data.session)
    } catch {
      reset()
      initialized.value = false
    }
  }

  async function authHeaders(): Promise<Record<string, string>> {
    if (runtimeMode !== 'production') return headersForRuntime(runtimeMode, localRole, null)
    if (!supabase) return {}
    const { data } = await supabase.auth.getSession()
    return headersForRuntime(runtimeMode, localRole, data.session?.access_token ?? null)
  }

  async function signInWithEmail(email: string) {
    if (!supabase || !clientRuntime) throw new Error('Supabase sign-in is not configured for this environment.')
    const { error } = await supabase.auth.signInWithOtp({
      email,
      options: { emailRedirectTo: `${window.location.origin}/` }
    })
    if (error) throw error
  }

  async function signOut() {
    reset()
    if (!supabase) return
    const { error } = await supabase.auth.signOut()
    if (error) throw error
  }

  if (clientRuntime) void initialize()

  return { user, role, ready, canEdit, isAdmin, initialize, authHeaders, signInWithEmail, signOut, reset }
}
