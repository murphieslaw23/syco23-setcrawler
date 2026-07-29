import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

const configPath = resolve(process.cwd(), 'playwright.config.ts')
const configSource = readFileSync(configPath, 'utf8')

describe('Playwright fixture runtime contract', () => {
  it('starts API and web servers with deterministic fixture settings', () => {
    expect(configSource).toContain('ENVIRONMENT=fixture REPOSITORY_MODE=memory AUTH_MODE=local')
    expect(configSource).toContain('NUXT_PUBLIC_RUNTIME_MODE=fixture NUXT_PUBLIC_API_BASE=http://127.0.0.1:8000')
  })
})
