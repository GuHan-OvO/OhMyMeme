import { expect, test } from '@playwright/test'
import { createServer } from 'node:http'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

const root = fileURLToPath(new URL('../../src/webui/', import.meta.url))
const files = new Map([
  ['/vue.html', ['vue.html', 'text/html; charset=utf-8']],
  ['/dist/ohmymeme.js', ['dist/ohmymeme.js', 'text/javascript; charset=utf-8']],
  ['/settings/', ['settings.html', 'text/html; charset=utf-8']],
  ['/settings.css', ['settings.css', 'text/css; charset=utf-8']],
  ['/settings.js', ['settings.js', 'text/javascript; charset=utf-8']],
])

function startServer() {
  const server = createServer((request, response) => {
    const asset = files.get(new URL(request.url ?? '/', 'http://127.0.0.1').pathname)
    if (!asset) {
      response.writeHead(404)
      response.end()
      return
    }
    response.writeHead(200, { 'Content-Type': asset[1] })
    response.end(readFileSync(`${root}${asset[0]}`))
  })
  return new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => resolve(server))
  })
}

test('main bridge rejects malformed init payload without breaking the shell', async ({ page }) => {
  const server = await startServer()
  const address = server.address()
  if (address === null || typeof address === 'string') throw new Error('server address unavailable')
  try {
    await page.addInitScript(() => {
      window.pywebview = { api: {
        get_init_data: async () => [],
        count_memes: async () => 0,
        search_memes: async () => [],
        get_tags: async () => [],
        get_collections: async () => [],
        rescan_cache: async () => true,
        run_auto_sync: async () => ({ fetched: false, synced: false, error: '' }),
        check_update: async () => ({ pending: false, has_update: false }),
      } }
    })
    await page.goto(`http://127.0.0.1:${address.port}/vue.html`)
    await expect(page.locator('#app-mount')).toBeVisible()
    await expect(page.locator('.sidebar-toggle')).toBeVisible()
  } finally {
    await page.close()
    server.closeAllConnections()
    await new Promise((resolve, reject) => server.close(error => (error ? reject(error) : resolve())))
  }
})

test('settings decoder turns malformed async result into a closed null result', async ({ page }) => {
  const server = await startServer()
  const address = server.address()
  if (address === null || typeof address === 'string') throw new Error('server address unavailable')
  try {
    await page.addInitScript(() => {
      window.pywebview = { api: { check_connectivity: async () => 'malformed' } }
    })
    await page.goto(`http://127.0.0.1:${address.port}/settings/`)
    const result = await page.evaluate(() => window.api('check_connectivity'))
    expect(result).toBeNull()
  } finally {
    await page.close()
    server.closeAllConnections()
    await new Promise((resolve, reject) => server.close(error => (error ? reject(error) : resolve())))
  }
})
