export function fixtureValue<T>(runtimeMode: string, fixture: T, empty: T): T {
  return runtimeMode === 'fixture' ? structuredClone(fixture) : structuredClone(empty)
}

export function shouldLoadOperationalData(runtimeMode: string, sessionReady: boolean) {
  return runtimeMode !== 'production' || sessionReady
}
