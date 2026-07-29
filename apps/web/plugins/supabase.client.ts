import { createClient, type SupabaseClient } from '@supabase/supabase-js'

import { useSycoRuntime } from '~/composables/useSycoRuntime'

export default defineNuxtPlugin(() => {
  const runtime = useSycoRuntime()
  const supabase: SupabaseClient | null = runtime.supabaseUrl && runtime.supabaseAnonKey
    ? createClient(runtime.supabaseUrl, runtime.supabaseAnonKey)
    : null

  return { provide: { supabase } }
})
