export type AppRole = 'viewer' | 'editor' | 'admin'
export type RuntimeMode = 'fixture' | 'local' | 'production'

export interface ApiIdentity {
  user_id: string
  role: string
}

export function capabilities(role: AppRole) {
  return {
    edit: role === 'editor' || role === 'admin',
    admin: role === 'admin'
  }
}

export function headersForRuntime(runtimeMode: string, localRole: AppRole, accessToken: string | null): Record<string, string> {
  if (runtimeMode !== 'production') return { 'X-Local-Role': localRole }
  return accessToken ? { Authorization: `Bearer ${accessToken}` } : {}
}

export function roleFromIdentity(identity: ApiIdentity): AppRole {
  return identity.role === 'editor' || identity.role === 'admin' || identity.role === 'viewer'
    ? identity.role
    : 'viewer'
}

export function canChangeSetStatus(role: AppRole, status: 'accepted' | 'rejected' | 'published') {
  return status === 'published' ? role === 'admin' : capabilities(role).edit
}

export function canManageProviders(role: AppRole) {
  return capabilities(role).admin
}
