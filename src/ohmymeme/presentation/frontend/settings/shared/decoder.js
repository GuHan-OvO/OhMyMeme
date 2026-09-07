/* Runtime decoder for the generated bridge schema. */
class BridgeDecodeError extends Error {
  constructor(method, message) {
    super(`bridge response rejected for ${method}: ${message}`);
    this.name = 'BridgeDecodeError';
    this.method = method;
  }
}

const bridgeBooleanMethods = new Set([
  'open_adb_folder', 'open_adb_help', 'qqnt_open_dir', 'start_download',
  'run_downloaded_installer', 'start_window_drag',
]);
const bridgeStringMethods = new Set(['lan_get_ip', 'get_current_version', 'sync_test']);
const bridgeVoidMethods = new Set([
  'save_settings', 'cancel_qq_import', 'cancel_tg_import', 'cancel_douyin_import',
  'cancel_wechat_import', 'qqnt_cancel', 'close_settings', 'move_window',
  'refresh_memes', 'refresh_tags', 'refresh_collections',
]);

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function decodeBridgeResult(method, value) {
  if (value && typeof value.then === 'function') {
    return value.then(
      (resolved) => {
        try { return decodeBridgeResult(method, resolved); }
        catch (error) { console.error(error); return null; }
      },
      (error) => { console.error(error); return null; },
    );
  }
  if (bridgeVoidMethods.has(method)) {
    if (value !== null && value !== undefined) throw new BridgeDecodeError(method, 'expected void');
    return null;
  }
  if (bridgeBooleanMethods.has(method)) {
    if (typeof value !== 'boolean') throw new BridgeDecodeError(method, 'expected boolean');
    return value;
  }
  if (bridgeStringMethods.has(method)) {
    if (typeof value !== 'string') throw new BridgeDecodeError(method, 'expected string');
    return value;
  }
  if (!isPlainObject(value)) throw new BridgeDecodeError(method, 'expected object');
  return value;
}
