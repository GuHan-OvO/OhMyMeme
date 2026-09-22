/* Plugins */
async function initPluginsPanel() {
  const root = document.getElementById('plugin-list');
  if (!root) return;
  try {
    const resp = await fetch('/api/plugins');
    if (!resp.ok) throw new Error(String(resp.status));
    const data = await resp.json();
    renderPluginList(root, data.plugins || []);
  } catch (error) {
    root.textContent = '插件列表加载失败';
  }
}

function renderPluginList(root, plugins) {
  if (!plugins.length) {
    root.textContent = '暂无插件';
    return;
  }
  const kinds = { source: '导入来源', sync: '同步', transport: '传输' };
  root.innerHTML = plugins.map((plugin) => {
    const kind = kinds[plugin.kind] || plugin.kind;
    const origin = plugin.origin === 'bundled' ? '官方' : '第三方';
    const meta = [kind, origin, plugin.version].filter(Boolean).join(' · ');
    const checked = plugin.enabled ? ' checked' : '';
    const versions = plugin.versions || [];
    const rollback = plugin.origin === 'bundled' || versions.length < 2 ? '' :
      '<select class="plugin-version" data-plugin-version="' + esc(plugin.id) + '">' +
      versions.map((v) => '<option value="' + esc(v) + '"' +
        (v === plugin.version ? ' selected' : '') + '>' + esc(v) + '</option>').join('') +
      '</select>' +
      '<button class="btn btn-secondary btn-sm" onclick="rollbackPlugin(\'' +
      esc(plugin.id) + '\')">切换</button>';
    const uninstall = plugin.origin === 'bundled' ? '' :
      '<button class="btn btn-danger-outline btn-sm" onclick="uninstallPlugin(\'' +
      esc(plugin.id) + '\')">卸载</button>';
    return '<div class="plugin-row">' +
      '<span class="plugin-info">' +
      '<span class="plugin-name">' + esc(plugin.label) + '</span>' +
      '<span class="plugin-meta">' + esc(plugin.id) + ' · ' + esc(meta) + '</span>' +
      '</span>' +
      '<span class="plugin-actions">' + rollback + uninstall +
      '<button class="btn btn-secondary btn-sm" onclick="reloadPlugin(\'' +
      esc(plugin.id) + '\')">重载</button>' +
      '<input type="checkbox" data-plugin-id="' + esc(plugin.id) + '"' + checked +
      ' aria-label="启用插件">' +
      '</span>' +
      '</div>';
  }).join('');
  root.querySelectorAll('input[data-plugin-id]').forEach((input) => {
    input.addEventListener('change', () => { _settingsDirty = true; });
  });
}

function collectDisabledPlugins() {
  return Array.from(document.querySelectorAll('#plugin-list input[data-plugin-id]'))
    .filter((input) => !input.checked)
    .map((input) => input.dataset.pluginId);
}

async function installPluginFile(input) {
  const file = input && input.files && input.files[0];
  if (!file) return;
  try {
    const buffer = await file.arrayBuffer();
    const resp = await fetch('/api/plugins/install', {
      method: 'POST',
      headers: { 'Content-Type': 'application/octet-stream' },
      body: buffer,
    });
    const data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || '安装失败');
    showToast('插件已安装：' + data.plugin + ' ' + data.version);
    initPluginsPanel();
  } catch (error) {
    showToast('安装失败：' + (error.message || error));
  } finally {
    input.value = '';
  }
}

async function uninstallPlugin(pluginId) {
  if (!(await showConfirm('卸载插件', '确定卸载 ' + pluginId + '？该插件目录会被删除，配置保留。'))) return;
  try {
    const resp = await fetch('/api/plugins/uninstall', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: pluginId }),
    });
    const data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || '卸载失败');
    showToast('插件已卸载');
    initPluginsPanel();
  } catch (error) {
    showToast('卸载失败：' + (error.message || error));
  }
}

async function reloadPlugin(pluginId) {
  try {
    const resp = await fetch('/api/plugins/reload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: pluginId }),
    });
    if (!resp.ok) throw new Error(String(resp.status));
    showToast('插件已重载，下次使用时按磁盘版本启动');
  } catch (error) {
    showToast('重载失败：' + (error.message || error));
  }
}

async function rollbackPlugin(pluginId) {
  const select = document.querySelector(
    'select[data-plugin-version="' + pluginId + '"]'
  );
  const version = select && select.value;
  if (!version) return;
  try {
    const resp = await fetch('/api/plugins/rollback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: pluginId, version }),
    });
    const data = await resp.json();
    if (!resp.ok || !data.ok) throw new Error(data.error || '切换失败');
    showToast('已切换到 ' + data.version + '，重启后生效');
    initPluginsPanel();
  } catch (error) {
    showToast('切换失败：' + (error.message || error));
  }
}
