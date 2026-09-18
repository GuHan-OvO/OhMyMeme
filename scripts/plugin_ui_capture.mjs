import { chromium } from '@playwright/test'
import { createHash } from 'node:crypto'
import { mkdir, readFile } from 'node:fs/promises'
import { relative, resolve } from 'node:path'

const args = new Map()
for (let index = 2; index < process.argv.length; index += 2) {
  args.set(process.argv[index], process.argv[index + 1])
}

const root = resolve(args.get('--repo-root'))
const projectionFile = resolve(args.get('--projection-file'))
const screenshotsDir = resolve(args.get('--screenshots-dir'))
const viewport = args.get('--viewport')
const [width, height] = viewport.split('x').map(Number)
const externalProbe = args.get('--external-probe')
  ? JSON.parse(args.get('--external-probe'))
  : null
const origin = 'http://fixture.local'
const routes = {
  '/settings.html': ['src', 'webui', 'settings.html'],
  '/settings.css': ['src', 'webui', 'settings.css'],
  '/settings.js': ['src', 'webui', 'settings.js'],
}
const contributorSvg = '<svg xmlns="http://www.w3.org/2000/svg" width="1" height="1" role="img" aria-label="fixture contributor"/>'
const blockedRequests = []
const consoleErrors = []

function hash(value) {
  return createHash('sha256').update(value).digest('hex')
}

function contentType(pathname) {
  if (pathname.endsWith('.css')) return 'text/css; charset=utf-8'
  if (pathname.endsWith('.js')) return 'application/javascript; charset=utf-8'
  if (pathname.endsWith('.json')) return 'application/json; charset=utf-8'
  return 'text/html; charset=utf-8'
}

function mockResult(method) {
  if (method === 'get_settings') {
    return {
      hotkey: 'Ctrl+Alt+M',
      auto_play_gif: true,
      copy_resize_mode: 1,
      ftp_path: '/',
      lan_port: 17852,
      record_recent_use: true,
      show_download_done: true,
      show_download_progress: true,
      show_startup_animation: true,
      show_uncategorized: true,
      show_upload_done: true,
      show_upload_progress: true,
      s3_addressing_style: 'virtual',
      s3_signature_version: 's3',
    }
  }
  if (method === 'get_storage_info') {
    return { cache_dir: '<fixture-cache>', custom: false, file_count: 0, total_size: 0 }
  }
  if (method === 'lan_get_status') return { clients: [], port: 17852, status: 'stopped' }
  if (method === 'lan_get_ip') return '127.0.0.1'
  if (method === 'get_current_version') return '0.0.0-fixture'
  if (method === 'check_connectivity') return { ok: true }
  return {}
}

function normalizeText(value) {
  return value
    .replace(/\b(?:\d{4}-\d{2}-\d{2}[T ][^"'< ]+|\d{10,})\b/g, '<timestamp>')
    .replace(/[A-Za-z]:\\[^"'< ]+/g, '<temporary>')
}

async function normalizedDom(locator) {
  return locator.evaluate((node) => {
    const copy = node.cloneNode(true)
    for (const element of copy.querySelectorAll('*')) {
      element.removeAttribute('data-v-app')
      element.removeAttribute('style')
      element.removeAttribute('data-timestamp')
      element.removeAttribute('data-testid')
      if (element instanceof HTMLInputElement) {
        element.setAttribute('value', element.defaultValue)
      }
    }
    return copy.outerHTML.replace(/\s+/g, ' ').trim()
  }).then(normalizeText)
}

async function screenshotFact(locator, selector) {
  const slug = selector.replace(/[^a-z0-9]+/gi, '-').replace(/^-|-$/g, '')
  const primary = resolve(screenshotsDir, `${slug}.png`)
  const repeat = resolve(screenshotsDir, `${slug}.repeat.png`)
  await locator.screenshot({ path: primary })
  await locator.screenshot({ path: repeat })
  const first = await readFile(primary)
  const second = await readFile(repeat)
  return {
    path: relative(root, primary).replaceAll('\\', '/'),
    repeat_path: relative(root, repeat).replaceAll('\\', '/'),
    sha256: hash(first),
    repeat_sha256: hash(second),
    stable: hash(first) === hash(second),
  }
}

let browser
let context
let browserVersion = ''
try {
  const projection = await readFile(projectionFile)
  await mkdir(screenshotsDir, { recursive: true })
  browser = await chromium.launch({
    headless: true,
    args: [
      '--disable-background-networking',
      '--disable-component-update',
      '--disable-domain-reliability',
      '--disable-features=MediaRouter,OptimizationHints,AutofillServerCommunication',
      '--disable-sync',
      '--no-default-browser-check',
      '--no-first-run',
      '--host-resolver-rules=MAP * 0.0.0.0',
    ],
  })
  browserVersion = browser.version()
  context = await browser.newContext({
    acceptDownloads: false,
    colorScheme: 'dark',
    deviceScaleFactor: 1,
    locale: 'zh-CN',
    reducedMotion: 'reduce',
    serviceWorkers: 'block',
    timezoneId: 'UTC',
    viewport: { width, height },
  })
  await context.addInitScript(() => {
    Object.defineProperty(window, 'pywebview', {
      configurable: true,
      value: {
        api: new Proxy({}, {
          get(_target, method) {
            return async () => window.__todo17MockResult(String(method))
          },
        }),
      },
    })
  })
  await context.addInitScript((settings) => {
    window.__todo17MockResult = (method) => settings[method] || {}
  }, {
    check_connectivity: mockResult('check_connectivity'),
    get_current_version: mockResult('get_current_version'),
    get_settings: mockResult('get_settings'),
    get_storage_info: mockResult('get_storage_info'),
    lan_get_ip: mockResult('lan_get_ip'),
    lan_get_status: mockResult('lan_get_status'),
  })
  await context.route('**/*', async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.origin !== origin) {
      blockedRequests.push({ kind: 'browser-request', target: request.url(), verdict: 'blocked' })
      await route.abort('blockedbyclient')
      return
    }
    if (url.pathname === '/api/plugin-ui') {
      await route.fulfill({ body: projection, contentType: 'application/json; charset=utf-8', status: 200 })
      return
    }
    if (url.pathname === '/api/contributors') {
      await route.fulfill({ body: contributorSvg, contentType: 'image/svg+xml; charset=utf-8', status: 200 })
      return
    }
    const file = routes[url.pathname]
    if (!file) {
      await route.fulfill({ body: 'not found', contentType: 'text/plain', status: 404 })
      return
    }
    const body = await readFile(resolve(root, ...file))
    await route.fulfill({ body, contentType: contentType(url.pathname), status: 200 })
  })
  const page = await context.newPage()
  page.on('console', (message) => {
    const text = message.text()
    if (message.type() === 'error' && !(externalProbe && text.includes('ERR_BLOCKED_BY_CLIENT'))) {
      consoleErrors.push(text)
    }
  })
  await page.goto(`${origin}/settings.html`, { waitUntil: 'networkidle' })
  await page.waitForFunction(() => {
    const row = document.querySelector('[onclick="startQQNTWizard()"] .import-name')
    return row && row.textContent === '电脑版 QQ（QQNT 本地缓存）'
  })
  await page.addStyleTag({
    content: '* { animation: none !important; caret-color: transparent !important; transition: none !important; }',
  })
  await page.evaluate(() => document.activeElement?.blur())
  if (externalProbe) {
    await page.evaluate(async (target) => {
      try { await fetch(target) } catch (_) {}
    }, externalProbe.target)
    await page.waitForTimeout(25)
  }
  const selectors = ['#titlebar', '#settings-nav', '#settings-content', '#toast']
  const regions = []
  for (const selector of selectors) {
    const locator = page.locator(selector)
    const count = await locator.count()
    if (count !== 1) throw new Error(`${selector}: expected one element, got ${count}`)
    const box = await locator.boundingBox()
    if (!box || box.width <= 0 || box.height <= 0) throw new Error(`${selector}: empty region`)
    regions.push({
      selector,
      dom_sha256: hash(await normalizedDom(locator)),
      rect: {
        height: Math.round(box.height),
        width: Math.round(box.width),
        x: Math.round(box.x),
        y: Math.round(box.y),
      },
      screenshot: await screenshotFact(locator, selector),
    })
  }
  const labels = await page.locator('.import-row .import-name').allTextContents()
  const documentHash = hash(await page.locator('#app').evaluate((node) => node.outerHTML.replace(/\s+/g, ' ').trim()))
  await context.close()
  await browser.close()
  process.stdout.write(JSON.stringify({
    browser: { name: 'chromium', version: browserVersion },
    cleanup: { browser_closed: true, server: 'not-started', pid: null },
    console_errors: consoleErrors,
    external_requests: blockedRequests,
    labels,
    settings: { document_sha256: documentHash, regions },
  }))
} catch (error) {
  try { if (context) await context.close() } catch (_) {}
  try { if (browser) await browser.close() } catch (_) {}
  process.stdout.write(JSON.stringify({
    cleanup: { browser_closed: true, server: 'not-started', pid: null },
    error: String(error && error.message ? error.message : error),
    external_requests: blockedRequests,
  }))
  process.exitCode = 1
}
