import { defineConfig, devices } from '@playwright/test'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

export default defineConfig({
  testDir: './tests/e2e',
  outputDir: join(
    tmpdir(),
    'ohmymeme-playwright-results',
    String(process.pid),
  ),
  timeout: 30_000,
  reporter: 'list',
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
})
