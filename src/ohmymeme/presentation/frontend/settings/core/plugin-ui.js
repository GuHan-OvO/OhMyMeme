/* Bounded host data for the existing screens; never selectors, markup or code. */
let pluginUI = null;

function samePluginUI(value, expected, field = 'ui') {
  if (typeof value !== typeof expected || value === null || expected === null) {
    if (value !== expected) throw new Error(field + ': unexpected type');
    return;
  }
  if (typeof expected !== 'object') {
    if (value !== expected) throw new Error(field + ': differs from fixed host data');
    return;
  }
  if (Array.isArray(value) !== Array.isArray(expected)) throw new Error(field + ': unexpected type');
  const keys = Object.keys(expected);
  for (const key of Object.keys(value)) {
    if (!Object.hasOwn(expected, key)) throw new Error(field + '.' + key + ': forbidden field');
  }
  for (const key of keys) {
    if (!Object.hasOwn(value, key)) throw new Error(field + '.' + key + ': missing field');
    samePluginUI(value[key], expected[key], field + '.' + key);
  }
}

function installPluginUI(value) {
  // Validate the whole snapshot before looking up nodes or binding host callbacks.
  samePluginUI(value, fixedPluginUI);
  const snapshot = JSON.parse(JSON.stringify(value));
  const rows = {
    qq: document.querySelector('[onclick="startQQNTWizard()"]'),
    telegram: document.querySelector('[onclick="openTGImportDialog()"]'),
    douyin: document.querySelector('[onclick="openDYImportDialog()"]'),
    wechat: document.querySelector('[onclick="openWechatImportDialog()"]'),
  };
  const handlers = { startQQNTWizard, openTGImportDialog, openDYImportDialog, openWechatImportDialog };
  const options = document.getElementById('s-sync-type').options;
  for (const contribution of snapshot.contributions) {
    if (Object.hasOwn(rows, contribution.icon)) {
      const row = rows[contribution.icon];
      row.querySelector('.import-name').textContent = contribution.label;
      row.onclick = handlers[contribution.handler];
    } else if (contribution.screen !== 'lan') {
      const option = Array.from(options).find(item => item.value === contribution.screen);
      option.textContent = contribution.label;
    } else {
      document.querySelector('[data-group="network"] .section-title').textContent = contribution.label;
    }
  }
  pluginUI = snapshot;
}

async function loadPluginUI() {
  // A read-only, same-origin host route, separate from Config and the Bridge ABI.
  if (pluginUI) return;
  const response = await fetch('/api/plugin-ui', { cache: 'no-store', credentials: 'same-origin' });
  if (!response.ok) throw new Error('ui: host projection unavailable');
  installPluginUI(await response.json());
}

function pluginUIAction(method) {
  // Caller names are existing host literals; data can never introduce a new method.
  const known = fixedPluginUI.contributions.flatMap(row => row.actions).some(row => row.action === method);
  if (!known) return null;
  if (!pluginUI) throw new Error('ui: host projection not ready');
  return pluginUI.contributions.flatMap(row => row.actions).find(row => row.action === method);
}

function pluginArgumentMatches(value, schema) {
  // Only the primitive schemas already present in the fixed Bridge are supported.
  if (schema.anyOf) return schema.anyOf.some(item => pluginArgumentMatches(value, item));
  if (schema.type === 'null') return value === null;
  if (schema.type === 'integer') return Number.isInteger(value);
  if (schema.type === 'number') return typeof value === 'number' && Number.isFinite(value);
  return (schema.type === 'string' || schema.type === 'boolean') && typeof value === schema.type;
}

function checkPluginUIArguments(method, args) {
  const action = pluginUIAction(method);
  if (!action) return;
  if (args.length > action.arguments.length) throw new Error('args: too many arguments');
  action.arguments.forEach((argument, index) => {
    if (index >= args.length && !argument.required) return;
    if (!pluginArgumentMatches(args[index], argument.schema)) {
      throw new Error('args.' + argument.name + ': invalid type');
    }
  });
}

function pluginUILabel(method) {
  return pluginUIAction(method).label;
}

function pluginProgressValue(method, state, field) {
  // Read the original field without replacing/filtering the public progress object.
  if (!pluginUIAction(method).progress_fields.includes(field)) {
    throw new Error('progress_fields.' + field + ': unavailable');
  }
  return state[field];
}
