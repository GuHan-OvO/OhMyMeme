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
    return '<label class="plugin-row">' +
      '<span class="plugin-info">' +
      '<span class="plugin-name">' + esc(plugin.label) + '</span>' +
      '<span class="plugin-meta">' + esc(plugin.id) + ' · ' + esc(meta) + '</span>' +
      '</span>' +
      '<input type="checkbox" data-plugin-id="' + esc(plugin.id) + '"' + checked + '>' +
      '</label>';
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
