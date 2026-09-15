import { readFileSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { runInContext } from 'node:vm'
import { JSDOM } from 'jsdom'
import { afterAll, describe, expect, it, vi } from 'vitest'

const root = process.cwd()
const html = readFileSync(resolve(root, 'src/webui/settings.html'), 'utf8')
const runtime = readFileSync(resolve(root, 'src/webui/settings.js'), 'utf8')
const data = JSON.parse(readFileSync(resolve(root, 'src/webui/plugin-ui.json'), 'utf8'))
const observations = []

function shell() {
  // Run only repository code in a local DOM, with no resource loader or network.
  const dom = new JSDOM(html, { runScripts: 'outside-only', url: 'http://127.0.0.1:17899/settings/' })
  const window = dom.window
  const calls = []
  const timers = []
  const results = {}
  window.fetch = vi.fn(async () => ({ ok: true, json: async () => structuredClone(data) }))
  window.console = { ...console, error: vi.fn() }
  window.setInterval = vi.fn(callback => { timers.push(callback); return timers.length })
  window.clearInterval = vi.fn()
  window.setTimeout = vi.fn()
  window.pywebview = { api: new Proxy({}, {
    get: (_, method) => (...args) => {
      calls.push({ method, args })
      if (Object.hasOwn(results, method)) return results[method]
      if (String(method).includes('cancel') || method === 'save_settings') return null
      if (method === 'lan_get_ip') return '127.0.0.1'
      if (method === 'sync_test') return 'ok'
      return { ok: true }
    },
  }) }
  // Avoid auto-init in tests that deliberately submit invalid snapshots.
  const addEventListener = window.document.addEventListener.bind(window.document)
  window.document.addEventListener = (name, ...args) => {
    if (name !== 'DOMContentLoaded') addEventListener(name, ...args)
  }
  runInContext(runtime, dom.getInternalVMContext())
  return { dom, window, calls, timers, results, close: () => dom.window.close() }
}

describe('production settings UI projection', () => {
  it('consumes all nine rows without changing labels, SVGs, screens or mobile QQ', async () => {
    const s = shell()
    try {
      const rows = [...s.window.document.querySelectorAll('.import-row')]
      const before = rows.map(row => ({ label: row.querySelector('.import-name').textContent, svg: row.querySelector('svg').outerHTML }))
      const mobile = rows[0].outerHTML
      await s.window.initSettings()
      expect(s.window.fetch).toHaveBeenCalledWith('/api/plugin-ui', { cache: 'no-store', credentials: 'same-origin' })
      expect(before.map(row => row.label)).toEqual(['手机版 QQ（ADB 拉取）', '电脑版 QQ（QQNT 本地缓存）', 'Telegram Desktop', '抖音', '微信'])
      const handlerNames = ['startQQNTWizard', 'openTGImportDialog', 'openDYImportDialog', 'openWechatImportDialog']
      expect(rows.slice(1).map(row => row.onclick.name)).toEqual(handlerNames)
      expect(rows.map(row => ({ label: row.querySelector('.import-name').textContent, svg: row.querySelector('svg').outerHTML }))).toEqual(before)
      expect(rows[0].outerHTML).toBe(mobile)
      expect([...s.window.document.getElementById('s-sync-type').options].map(row => [row.value, row.textContent])).toEqual([
        ['', '无'], ['ftp', 'FTP'], ['s3', 'S3 兼容存储'], ['r2', 'Cloudflare R2'], ['webdav', 'WebDAV'],
      ])
      for (const type of ['ftp', 's3', 'r2', 'webdav']) {
        s.window.document.getElementById('s-sync-type').value = type
        s.window.toggleSyncType()
        for (const panel of ['ftp', 's3', 'r2', 'webdav']) {
          expect(s.window.document.getElementById('s-sync-' + panel).style.display).toBe(panel === type ? 'block' : 'none')
        }
      }
      for (let i = 1; i < rows.length; i++) await rows[i].onclick()
      for (const overlay of ['qqnt-overlay', 'tg-import-overlay', 'dy-import-overlay', 'wechat-import-overlay']) {
        expect(s.window.document.getElementById(overlay).style.display).toBe('flex')
      }
      expect(s.calls.some(call => call.method === 'qqnt_check_env')).toBe(true)
      observations.push({ case: 'nine-providers', rows: before, handlers: handlerNames, bridge_calls: s.calls })
    } finally { s.close() }
  })

  it.each([
    ['label', 'Other non-empty label'], ['label', true], ['label', ''], ['icon', '<svg onload=run()>'],
    ['handler', 'eval'], ['provider', 'source.unknown'], ['provider', 'qq.mobile'],
    ['script', 'run()'], ['html', '<img onerror=run()>'], ['javascript', 'run()'],
    ['bridge_methods', ['execute']], ['extra', {}], ['screen', 'body'],
  ])('rejects %s before DOM/Bridge changes', (field, value) => {
    const s = shell()
    try {
      const original = s.window.document.documentElement.outerHTML
      const input = structuredClone(data)
      input.contributions[0][field] = value
      expect(() => s.window.installPluginUI(input)).toThrow(field)
      expect(s.window.document.documentElement.outerHTML).toBe(original)
      expect(s.calls).toEqual([])
      observations.push({ case: 'invalid-' + field, rejected: true, bridge_calls: [...s.calls], dom_unchanged: original === s.window.document.documentElement.outerHTML })
    } finally { s.close() }
  })

  it.each([
    ['action', 'execute'], ['label', 'Misleading PASS'], ['arguments', []],
    ['progress_fields', ['html']], ['script', 'run()'], ['args', [true]],
  ])('rejects action %s and preserves an already installed snapshot', (field, value) => {
    const s = shell()
    try {
      s.window.installPluginUI(data)
      const original = s.window.document.documentElement.outerHTML
      const input = structuredClone(data)
      const action = input.contributions[1].actions.find(row => row.action === 'start_tg_import')
      action[field] = value
      expect(() => s.window.installPluginUI(input)).toThrow(field)
      expect(s.window.document.documentElement.outerHTML).toBe(original)
      expect(s.calls).toEqual([])
    } finally { s.close() }
  })

  it('validates actual call arguments before invoking the Bridge, without changing defaults', async () => {
    const s = shell()
    try {
      s.window.installPluginUI(data)
      for (const [method, args] of [
        ['lan_start', [true, '']], ['lan_start', [1.5, '']],
        ['start_tg_import', [null, '', 1]], ['start_douyin_import', []],
        ['sync_push', ['true']], ['qqnt_check_env', ['extra']],
      ]) {
        expect(s.window.api(method, ...args)).toBeNull()
      }
      expect(s.calls).toEqual([])
      await s.window.api('start_tg_import')
      await s.window.api('sync_push')
      await s.window.api('lan_start', 17852, '')
      expect(s.calls).toEqual([
        { method: 'start_tg_import', args: [] }, { method: 'sync_push', args: [] },
        { method: 'lan_start', args: [17852, ''] },
      ])
      observations.push({ case: 'args', accepted: s.calls, rejected_calls: 0 })
    } finally { s.close() }
  })

  it('runs the four original start/progress/cancel controllers using projected progress fields', async () => {
    const s = shell()
    try {
      s.window.installPluginUI(data)
      const document = s.window.document
      document.getElementById('s-dy-cookie').value = 'fixture-cookie'
      document.getElementById('s-tg-tdata').value = 'fixture-tdata'
      document.getElementById('s-tg-passcode').value = 'ephemeral-passcode'
      s.results.get_tg_import_progress = { status: 'converting', progress: 37, message: 'tg-progress', elapsed_s: 3 }
      s.results.get_douyin_import_progress = { status: 'running', progress: 38, message: 'dy-progress' }
      s.results.get_wechat_import_progress = { status: 'running', progress: 39, message: 'wx-progress' }
      s.results.qqnt_get_progress = { status: 'copying', progress: 40, message: 'qq-progress', log: ['log'] }
      await s.window.startTGImport()
      await s.window.startDYImport()
      await s.window.startWechatImport()
      runInContext("qqnt.qq = '10001'; qqnt.output_dir = 'fixture-output';", s.dom.getInternalVMContext())
      await s.window.qqntStartExtract()
      for (const timer of s.timers) await timer()
      for (const [id, value] of [['tg-import', 37], ['dy-import', 38], ['wechat-import', 39], ['qqnt-progress', 40]]) {
        expect(document.getElementById(id + '-bar').style.width).toBe(value + '%')
      }
      s.window.closeTGOverlay()
      s.window.closeDYOverlay()
      s.window.closeWechatOverlay()
      s.window.qqntClose()
      expect(s.calls.filter(call => call.method.includes('cancel')).map(call => call.method)).toEqual([
        'cancel_tg_import', 'cancel_douyin_import', 'cancel_wechat_import', 'qqnt_cancel',
      ])
      expect(s.calls.find(call => call.method === 'start_tg_import').args).toEqual(['fixture-tdata', 'ephemeral-passcode', true])
      expect(s.calls.find(call => call.method === 'qqnt_start').args).toEqual(['10001', 'fixture-output', false, false])
      observations.push({ case: 'source-progress', widths: ['tg-import', 'dy-import', 'wechat-import', 'qqnt-progress'].map(id => document.getElementById(id + '-bar').style.width), methods: s.calls.map(call => call.method) })
    } finally { s.close() }
  })

  it('runs the sync progress and LAN controllers through the same fixed API', async () => {
    const s = shell()
    try {
      s.window.installPluginUI(data)
      const document = s.window.document
      document.getElementById('s-sync-type').value = 'ftp'
      document.getElementById('s-ftp-host').value = 'fixture.invalid'
      document.getElementById('s-show-up-progress').checked = true
      s.results.get_sync_progress = { status: 'uploading', current_file: 'one.webp', progress: 41, speed: 123 }
      let finish
      s.results.sync_push = new Promise(resolve => { finish = resolve })
      const pending = s.window.syncPush()
      // Advance only promise microtasks; no clock, socket or browser is started.
      for (let i = 0; i < 10 && !s.timers.length; i++) await Promise.resolve()
      expect(s.timers).toHaveLength(1)
      await s.timers[0]()
      expect(document.getElementById('sync-progress-bar').style.width).toBe('41%')
      expect(document.getElementById('sync-progress-file').textContent).toBe('one.webp')
      finish({ ok: true, uploaded: 1 })
      await pending
      s.results.lan_get_status = { status: 'running', port: 17852, clients: [], allow_secret_config: false }
      document.getElementById('s-lan-enable').checked = true
      await s.window.toggleLan()
      expect(s.calls.find(call => call.method === 'lan_start').args).toEqual([17852, ''])
      expect(document.getElementById('lan-status').textContent).toContain('运行中')
      observations.push({ case: 'sync-lan', progress: document.getElementById('sync-progress-bar').style.width, lan: document.getElementById('lan-status').textContent, methods: s.calls.map(call => call.method) })
    } finally { s.close() }
  })
})

afterAll(() => {
  if (process.env.OHMM_UI_REPORT) {
    writeFileSync(resolve(root, process.env.OHMM_UI_REPORT), JSON.stringify({ observations, scope: 'jsdom unit tests of production settings.js; no browser or network' }, null, 2))
  }
})
