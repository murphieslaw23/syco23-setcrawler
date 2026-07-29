export function useApi() {
  const { apiBase, runtimeMode } = useSycoRuntime()
  const { authHeaders } = useAuth()

  async function get<T>(path: string, fallback?: T): Promise<T> {
    try {
      return await $fetch<T>(path, { baseURL: apiBase, headers: await authHeaders(), timeout: 5_000 })
    } catch (error) {
      if (runtimeMode === 'fixture' && fallback !== undefined) return structuredClone(fallback)
      throw error
    }
  }

  async function send<T>(
    path: string,
    method: 'POST' | 'PATCH' | 'DELETE',
    body?: Record<string, unknown>
  ): Promise<T> {
    return await $fetch<T>(path, { baseURL: apiBase, headers: await authHeaders(), method, body, timeout: 5_000 })
  }

  return { get, send, apiBase }
}
