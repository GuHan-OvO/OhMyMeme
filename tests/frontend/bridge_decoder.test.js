import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { runInNewContext } from 'node:vm'
import { resolve } from 'node:path'

const source = readFileSync(
  resolve(process.cwd(), 'src/ohmymeme/presentation/frontend/settings/shared/decoder.js'),
  'utf8',
)

function decoder() {
  const context = { console: { error: () => undefined } }
  runInNewContext(`${source}; globalThis.decodeBridgeResult = decodeBridgeResult`, context)
  return context.decodeBridgeResult
}

describe('settings bridge decoder', () => {
  it('preserves boolean and void method results', () => {
    const decode = decoder()
    expect(decode('start_window_drag', true)).toBe(true)
    expect(decode('refresh_memes', null)).toBeNull()
  })

  it('rejects malformed boolean results', () => {
    const decode = decoder()
    try {
      decode('start_window_drag', { ok: true })
      throw new Error('malformed boolean was accepted')
    } catch (error) {
      expect(error.name).toBe('BridgeDecodeError')
    }
  })
})
