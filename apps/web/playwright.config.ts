import { chromium, defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 30_000,
  fullyParallel: false,
  workers: 1,
  reporter: 'line',
  use: {
    baseURL: 'http://127.0.0.1:3000',
    colorScheme: 'dark',
    screenshot: 'only-on-failure',
    launchOptions: {
      executablePath: chromium.executablePath()
    }
  },
  webServer: [
    {
      command: 'ENVIRONMENT=fixture REPOSITORY_MODE=memory AUTH_MODE=local PYTHONPATH=../api ../../.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000',
      url: 'http://127.0.0.1:8000/health',
      reuseExistingServer: false,
      timeout: 30_000
    },
    {
      command: 'NUXT_PUBLIC_RUNTIME_MODE=fixture NUXT_PUBLIC_API_BASE=http://127.0.0.1:8000 NITRO_HOST=127.0.0.1 NITRO_PORT=3000 node .output/server/index.mjs',
      url: 'http://127.0.0.1:3000/inbox',
      reuseExistingServer: false,
      timeout: 30_000
    }
  ]
})
