const controlsRoot = document.querySelector('#controls');
const deviceLabel = document.querySelector('#device');
const errorBox = document.querySelector('#error');
const noticeBox = document.querySelector('#notice');
const refreshButton = document.querySelector('#refresh');
const resetButton = document.querySelector('#reset');

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = !message;
}

function showNotice(message) {
  noticeBox.textContent = message;
  noticeBox.hidden = !message;
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: {
      'Content-Type': 'application/json',
      'X-Requested-With': 'camera-controls',
    },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.error || `Request failed (${response.status})`);
  }
  return body;
}

function controlInput(control) {
  let input;
  if (control.type === 'bool') {
    input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = Boolean(control.value);
  } else if (control.type === 'menu' || control.type === 'intmenu') {
    input = document.createElement('select');
    Object.entries(control.menu).forEach(([value, label]) => {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = `${value}: ${label}`;
      option.selected = Number(value) === control.value;
      input.append(option);
    });
  } else {
    input = document.createElement('input');
    input.type = 'range';
    input.min = control.min;
    input.max = control.max;
    input.step = control.step;
    input.value = control.value;
  }
  input.id = `control-${control.name}`;
  input.disabled = control.inactive;
  input.dataset.name = control.name;
  return input;
}

async function applyControl(control, input) {
  const value = control.type === 'bool' ? input.checked : Number(input.value);
  input.disabled = true;
  showError('');
  try {
    const state = await requestJson(`/api/controls/${encodeURIComponent(control.name)}`, {
      method: 'PUT',
      body: JSON.stringify({value}),
    });
    renderState(state);
  } catch (error) {
    showError(error.message);
    await loadControls();
  }
}

function renderControl(control) {
  const row = document.createElement('div');
  row.className = `control-row${control.inactive ? ' inactive' : ''}`;

  const heading = document.createElement('div');
  heading.className = 'control-heading';
  const label = document.createElement('label');
  label.htmlFor = `control-${control.name}`;
  label.textContent = control.label;
  const value = document.createElement('output');
  value.textContent = control.value;
  heading.append(label, value);

  const input = controlInput(control);
  if (control.type === 'int' || control.type === 'int64') {
    input.addEventListener('input', () => { value.textContent = input.value; });
    input.addEventListener('change', () => applyControl(control, input));
  } else {
    input.addEventListener('change', () => applyControl(control, input));
  }

  const metadata = document.createElement('small');
  const defaultText = control.default_valid ? `default ${control.default}` : 'camera default is invalid';
  metadata.textContent = `${control.name} · ${defaultText}${control.inactive ? ' · controlled automatically' : ''}`;
  row.append(heading, input, metadata);
  return row;
}

function renderState(state) {
  deviceLabel.textContent = state.device;
  controlsRoot.replaceChildren();
  const groups = new Map();
  state.controls.forEach(control => {
    if (!groups.has(control.group)) groups.set(control.group, []);
    groups.get(control.group).push(control);
  });
  groups.forEach((controls, groupName) => {
    const group = document.createElement('section');
    group.className = 'control-group';
    const title = document.createElement('h3');
    title.textContent = groupName;
    group.append(title, ...controls.map(renderControl));
    controlsRoot.append(group);
  });
}

async function loadControls() {
  refreshButton.disabled = true;
  showError('');
  try {
    renderState(await requestJson('/api/controls'));
  } catch (error) {
    showError(error.message);
  } finally {
    refreshButton.disabled = false;
  }
}

refreshButton.addEventListener('click', loadControls);
resetButton.addEventListener('click', async () => {
  if (!window.confirm('Reset controls with valid camera defaults?')) return;
  resetButton.disabled = true;
  showError('');
  showNotice('');
  try {
    const state = await requestJson('/api/reset', {method: 'POST'});
    renderState(state);
    const skipped = state.reset.skipped.length;
    const errors = state.reset.errors.length;
    showNotice(`Reset ${state.reset.applied.length} controls; skipped ${skipped} invalid or inactive defaults; ${errors} errors.`);
  } catch (error) {
    showError(error.message);
  } finally {
    resetButton.disabled = false;
  }
});

loadControls();
