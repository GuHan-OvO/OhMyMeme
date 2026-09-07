import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { createServer } from 'node:http'
import { fileURLToPath } from 'node:url'

const staticAssets = new Map([
  [
    '/vue.html',
    [
      fileURLToPath(new URL('../../src/webui/vue.html', import.meta.url)),
      'text/html; charset=utf-8',
    ],
  ],
  [
    '/dist/ohmymeme.js',
    [
      fileURLToPath(
        new URL('../../src/webui/dist/ohmymeme.js', import.meta.url),
      ),
      'text/javascript; charset=utf-8',
    ],
  ],
])

test('collapses the real Vue main-window sidebar after its toggle is clicked', async ({ page }, testInfo) => {
  const server = createServer((request, response) => {
    const pathname = new URL(
      request.url ?? '/',
      'http://127.0.0.1',
    ).pathname
    const asset = staticAssets.get(pathname)
    if (asset === undefined) {
      response.writeHead(404)
      response.end()
      return
    }
    response.writeHead(200, { 'Content-Type': asset[1] })
    response.end(readFileSync(asset[0]))
  })

  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', resolve)
  })
  const address = server.address()
  if (address === null || typeof address === 'string') {
    throw new Error('The static browser test server has no TCP address')
  }

  try {
    await page.addInitScript(() => {
      window.pywebview = {
        api: {
          get_init_data: async () => ({
            memes: [],
            tags: [],
            collections: [],
            show_startup_animation: false,
            startup_bg_color: '#000000',
          }),
          count_memes: async () => 0,
          search_memes: async () => [],
          get_tags: async () => [],
          get_collections: async () => [],
          rescan_cache: async () => true,
          run_auto_sync: async () => true,
          check_update: async () => ({ pending: false, has_update: false }),
        },
      }
    })

    await page.goto(`http://127.0.0.1:${address.port}/vue.html`)
    const sidebar = page.locator('#sidebar')
    await expect(page.locator('.sidebar-toggle')).toBeVisible()
    await expect(sidebar).not.toHaveClass(/collapsed/)

    await page.locator('.sidebar-toggle').click()

    await expect(sidebar).toHaveClass(/collapsed/)
    await page.screenshot({ path: testInfo.outputPath('main-window.png') })
  } finally {
    await new Promise((resolve, reject) => {
      server.close(error => (error ? reject(error) : resolve()))
    })
  }
})
