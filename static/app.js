const state = {
  ports: [],
  connected: {
    pump_a: false,
    pump_b: false,
    valco: false,
  },
  active: {
    pump_a: false,
    pump_b: false,
  },
  valcoPosition: null,
  valcoChanging: false,
  connectedPreview: false,
  mode: 'pump_only',
  bedVolumeMl: 100,
  calibration: { m: 0.1694, b: -0.727 },
  solutions: Array.from({ length: 6 }, (_, index) => ({
    position: index + 1,
    name: '',
    category: 'Other',
  })),
  channels: Array.from({ length: 8 }, (_, index) => ({
    channel: index + 1,
    name: '',
    category: 'Other',
  })),
  valcoOutputs: Array.from({ length: 6 }, (_, index) => ({
    position: index + 1,
    label: index === 0 ? 'Waste' : '',
  })),
  steps: [],
  running: false,
  currentStepIndex: null,
  currentStepDuration: null,
  timeRemaining: null,
  lastError: '',
  lastMessage: '',
  lastUiMode: null,
  lastUiSolutions: '',
  settingsLoaded: false,
  statusSampleAt: null,
  isPaused: false,
};

const el = {};
let draggedStepId = null;
let valveChangeTimer = null;

function makeId() {
  return (crypto.randomUUID && crypto.randomUUID()) || `step-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function stepTemplate() {
  return {
    id: makeId(),
    step_type: 'flow',
    volume: '',
    volume_unit: 'mL',
    solution_position: '',
    solution_name: '',
    flow_rate: '',
    flow_unit: 'mL/min',
    direction: 'CW',
    channels: [1, 2, 3, 4, 5, 6, 7, 8],
    primary_channel: 1,
    valco_output_position: 1,
  };
}

function normalizeDirection(value) {
  const v = String(value || 'CW').toUpperCase();
  return v === 'CW' ? 'CW' : 'CCW';
}

function normalizeChannels(channels) {
  if (!Array.isArray(channels) || channels.length === 0) {
    return availableChannels();
  }
  const normalized = [];
  channels.forEach((value) => {
    const channel = Number(value);
    if (Number.isInteger(channel) && channel >= 1 && channel <= 8 && !normalized.includes(channel)) {
      normalized.push(channel);
    }
  });
  if (!normalized.length) return availableChannels();
  return normalized.sort((a, b) => a - b);
}

function clampNumber(value) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function availableChannels() {
  return state.channels
    .filter((channel) => (channel.channel <= 4 ? state.connected.pump_a : state.connected.pump_b))
    .map((channel) => channel.channel);
}

function availableChannelOptionsHtml(selectedValue) {
  const available = availableChannels();
  if (!available.length) {
    return '<option value="">No pump channels available</option>';
  }
  return state.channels
    .filter((channel) => available.includes(channel.channel))
    .map((channel) => {
      const label = channel.name ? `${channel.channel}: ${channel.name}` : `${channel.channel}: (unassigned)`;
      const selected = String(selectedValue || '') === String(channel.channel) ? 'selected' : '';
      return `<option value="${channel.channel}" ${selected}>${escapeHtml(label)}</option>`;
    })
    .join('');
}

function modeKind() {
  const hasPump = state.connected.pump_a || state.connected.pump_b;
  if (state.mode === 'channel_select' && state.connected.valco && hasPump) {
    return 'channel_select';
  }
  if (state.mode === 'pump_only' && (!state.connected.valco || !hasPump)) {
    return 'pump_only';
  }
  return 'pump_only';
}

function modeLabel() {
  const mode = modeKind();
  if (mode === 'channel_select') return 'Channel-Select Mode';
  return 'Pump-Only Mode';
}

function applyConnectedPreview() {
  if (!state.connectedPreview) {
    return;
  }
  state.connected.pump_a = true;
  state.connected.pump_b = true;
  state.connected.valco = true;
  state.valcoPosition = state.valcoPosition || 1;
  if (state.mode !== 'channel_select') {
    state.mode = 'channel_select';
  }
}

function bedVolume() {
  return clampNumber(el.bedVolumeInput.value || state.bedVolumeMl || 0);
}

function stepFlowMlpMin(step) {
  const flow = clampNumber(step.flow_rate);
  if (step.flow_unit === 'BV/hr') {
    return flow * bedVolume() / 60;
  }
  if (step.flow_unit === 'RPM') {
    const m = clampNumber(el.calibrationSlopeInput.value || state.calibration.m);
    const b = clampNumber(el.calibrationInterceptInput.value || state.calibration.b);
    return m * Math.min(flow, 100) + b;
  }
  return flow;
}

function stepVolumeMl(step) {
  const volume = clampNumber(step.volume);
  if (step.volume_unit === 'BV') {
    return volume * bedVolume();
  }
  return volume;
}

function stepRpm(step) {
  const m = clampNumber(el.calibrationSlopeInput.value || state.calibration.m);
  const b = clampNumber(el.calibrationInterceptInput.value || state.calibration.b);
  if (step.flow_unit === 'RPM') {
    return Math.min(clampNumber(step.flow_rate), 100);
  }
  const flow = stepFlowMlpMin(step);
  if (m === 0) {
    return null;
  }
  return (flow - b) / m;
}

function cappedFlowRateForUnit(unit) {
  const m = clampNumber(el.calibrationSlopeInput.value || state.calibration.m);
  const b = clampNumber(el.calibrationInterceptInput.value || state.calibration.b);
  const maxFlowMlpMin = m * 100 + b;
  if (unit === 'RPM') return 100;
  if (unit === 'BV/hr') {
    const bed = bedVolume();
    return bed > 0 ? maxFlowMlpMin * 60 / bed : 0;
  }
  return maxFlowMlpMin;
}

function capStepFlowRate(step) {
  if (!step || (step.step_type || 'flow') === 'pause') {
    return false;
  }
  const rpm = stepRpm(step);
  if (rpm === null || rpm <= 100) {
    return false;
  }
  const capped = Math.max(0, cappedFlowRateForUnit(step.flow_unit || 'mL/min'));
  step.flow_rate = Number.isFinite(capped) ? Number(capped.toFixed(3)) : 0;
  return true;
}

function solutionOptionsHtml(selectedValue) {
  return state.solutions.map((solution) => {
    const label = solution.name ? `${solution.position}: ${solution.name}` : `${solution.position}: (unassigned)`;
    const selected = String(selectedValue || '') === String(solution.position) ? 'selected' : '';
    return `<option value="${solution.position}" ${selected}>${escapeHtml(label)}</option>`;
  }).join('');
}

function channelOptionsHtml(selectedValue) {
  return state.channels.map((channel) => {
    const label = channel.name ? `${channel.channel}: ${channel.name}` : `${channel.channel}: (unassigned)`;
    const selected = String(selectedValue || '') === String(channel.channel) ? 'selected' : '';
    return `<option value="${channel.channel}" ${selected}>${escapeHtml(label)}</option>`;
  }).join('');
}

function valcoOutputOptionsHtml(selectedValue) {
  return state.valcoOutputs.map((output) => {
    const label = output.label ? `${output.position}: ${output.label}` : `${output.position}: (unlabeled)`;
    const selected = String(selectedValue || '') === String(output.position) ? 'selected' : '';
    return `<option value="${output.position}" ${selected}>${escapeHtml(label)}</option>`;
  }).join('');
}


function escapeHtml(text) {
  return String(text)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function renderSolutions() {
  el.solutionsGrid.innerHTML = state.solutions.map((solution) => `
    <div class="solution-card">
      <div class="panel-kicker">Position ${solution.position}</div>
      <div class="solution-fields">
        <label>
          <span>Solution Name</span>
          <input data-solution-field="name" data-position="${solution.position}" type="text" value="${escapeHtml(solution.name)}" placeholder="1.0 N HCl">
        </label>
        <label>
          <span>Category</span>
          <select data-solution-field="category" data-position="${solution.position}">
            <option value="Acid" ${solution.category === 'Acid' ? 'selected' : ''}>Acid</option>
            <option value="Base" ${solution.category === 'Base' ? 'selected' : ''}>Base</option>
            <option value="Other" ${solution.category === 'Other' ? 'selected' : ''}>Other</option>
          </select>
        </label>
      </div>
    </div>
  `).join('');

  el.solutionsGrid.querySelectorAll('[data-solution-field]').forEach((field) => {
    field.addEventListener('input', handleSolutionChange);
    field.addEventListener('change', handleSolutionChange);
  });
}

function renderChannelConfig() {
  el.channelsGrid.innerHTML = state.channels.map((channel) => `
    <div class="solution-card">
      <div class="panel-kicker">Channel ${channel.channel}</div>
      <div class="solution-fields channel-assignment-fields">
        <label>
          <span>Solution Name</span>
          <input data-channel-field="name" data-channel="${channel.channel}" type="text" value="${escapeHtml(channel.name)}" placeholder="Buffer A">
        </label>
        <label>
          <span>Category</span>
          <select data-channel-field="category" data-channel="${channel.channel}">
            <option value="Acid" ${channel.category === 'Acid' ? 'selected' : ''}>Acid</option>
            <option value="Base" ${channel.category === 'Base' ? 'selected' : ''}>Base</option>
            <option value="Other" ${channel.category === 'Other' ? 'selected' : ''}>Other</option>
          </select>
        </label>
      </div>
    </div>
  `).join('');

  el.channelsGrid.querySelectorAll('[data-channel-field]').forEach((field) => {
    field.addEventListener('input', handleChannelConfigChange);
    field.addEventListener('change', handleChannelConfigChange);
  });
}

function renderValcoOutputs() {
  el.valcoOutputsGrid.innerHTML = state.valcoOutputs.map((output) => `
    <div class="solution-card">
      <div class="panel-kicker">Valco Output ${output.position}</div>
      <label>
        <span>Label</span>
        <input data-valco-output-field="label" data-position="${output.position}" type="text" value="${escapeHtml(output.label)}" placeholder="Waste">
      </label>
    </div>
  `).join('');

  el.valcoOutputsGrid.querySelectorAll('[data-valco-output-field]').forEach((field) => {
    field.addEventListener('input', handleValcoOutputChange);
    field.addEventListener('change', handleValcoOutputChange);
  });
}

function renderSteps() {
  const mode = modeKind();
  const channelSelectMode = mode === 'channel_select';
  const pumpOnlyMode = mode === 'pump_only';

  el.stepsContainer.innerHTML = state.steps.map((step) => {
    step.direction = normalizeDirection(step.direction);
    step.channels = normalizeChannels(step.channels);
    capStepFlowRate(step);
    const rpm = stepRpm(step);
    const rpmLabel = rpm === null ? 'RPM unavailable' : `${rpm.toFixed(1)} RPM`;
    const rpmClass = rpm !== null && rpm > 100 ? 'rpm-display warning' : 'rpm-display';
    const channelsLabel = `ch:${step.channels.join(',')}`;
    const isPause = step.step_type === 'pause';

    return `
      <div class="step-row" data-step-id="${step.id}" draggable="true">
        <div class="drag-handle">⠿</div>
        <div class="step-fields ${pumpOnlyMode ? 'pump-only' : ''}">
          <label>
            <span>Type</span>
            <select data-field="step_type">
              <option value="flow" ${step.step_type === 'flow' ? 'selected' : ''}>Flow</option>
              <option value="pause" ${step.step_type === 'pause' ? 'selected' : ''}>Pause</option>
            </select>
          </label>
          ${!isPause ? `
          <label>
            <span>Flow</span>
            <input data-field="volume" type="number" min="0" step="0.001" value="${escapeHtml(String(step.volume || ''))}">
          </label>
          <label>
            <span>Volume Unit</span>
            <select data-field="volume_unit">
              <option value="mL" ${ (step.volume_unit || 'mL') === 'mL' ? 'selected' : ''}>mL</option>
              <option value="BV" ${ (step.volume_unit || 'mL') === 'BV' ? 'selected' : ''}>BV</option>
            </select>
          </label>
          ${pumpOnlyMode ? `
            <label>
              <span>Solution (Channel)</span>
              <select data-field="primary_channel">
                ${availableChannelOptionsHtml(step.primary_channel)}
              </select>
            </label>
          ` : ''}
          ${channelSelectMode ? `
            <label>
              <span>Primary Channel</span>
              <select data-field="primary_channel">
                ${availableChannelOptionsHtml(step.primary_channel)}
              </select>
            </label>
          ` : ''}
          <label>
            <span>at</span>
            <input data-field="flow_rate" type="number" min="0" step="0.001" value="${escapeHtml(String(step.flow_rate || ''))}">
          </label>
          <label>
            <span>Rate Unit</span>
            <select data-field="flow_unit">
              <option value="mL/min" ${ (step.flow_unit || 'mL/min') === 'mL/min' ? 'selected' : ''}>mL/min</option>
              <option value="BV/hr" ${ (step.flow_unit || 'mL/min') === 'BV/hr' ? 'selected' : ''}>BV/hr</option>
              <option value="RPM" ${ (step.flow_unit || 'mL/min') === 'RPM' ? 'selected' : ''}>RPM</option>
            </select>
          </label>
          ${channelSelectMode ? `
            <label>
              <span>Effluent To</span>
              <select data-field="valco_output_position">
                ${valcoOutputOptionsHtml(step.valco_output_position)}
              </select>
            </label>
          ` : ''}
          <label>
            <span>Direction</span>
            <select data-field="direction">
              <option value="CCW" ${step.direction === 'CCW' ? 'selected' : ''}>CCW</option>
              <option value="CW" ${step.direction === 'CW' ? 'selected' : ''}>CW</option>
            </select>
          </label>

          <div class="${rpmClass}">${rpmLabel}</div>
          ` : `
          <label>
            <span>Duration (s)</span>
            <input data-field="duration" type="number" min="0" step="0.1" value="${escapeHtml(String(step.duration || ''))}">
          </label>
          <div style="flex-grow: 1"></div>
          <div class="rpm-display">Pause Step</div>
          `}
        </div>
        <div class="step-actions">
          <button type="button" data-action="up">▲</button>
          <button type="button" data-action="down">▼</button>
          <button type="button" data-action="delete" class="danger-button">✕</button>
        </div>
        <div class="progress-container">
          <div class="progress-bar" data-step-id="${step.id}"></div>
        </div>
      </div>
    `;
  }).join('');

  el.stepsContainer.querySelectorAll('[data-field]').forEach((field) => {
    field.addEventListener('input', handleStepChange);
    field.addEventListener('change', handleStepChange);
  });

  el.stepsContainer.querySelectorAll('[data-action]').forEach((button) => {
    button.addEventListener('click', handleStepAction);
  });

  el.stepsContainer.querySelectorAll('.step-row').forEach(row => {
    row.addEventListener('dragstart', handleDragStart);
    row.addEventListener('dragover', handleDragOver);
    row.addEventListener('drop', handleDrop);
    row.addEventListener('dragend', handleDragEnd);
  });

  if (pumpOnlyMode) {
    el.methodWarning.textContent = 'Solution references are hidden in Pump-Only Mode.';
  } else if (channelSelectMode) {
    el.methodWarning.textContent = 'Channel-Select Mode is active; flow rates above 100 RPM are capped automatically.';
  }
  updateMethodTotals();
}

function updateMethodTotals() {
  let totalMinutes = 0;
  let totalVolumeMl = 0;

  state.steps.forEach((step) => {
    if ((step.step_type || 'flow') === 'pause') {
      totalMinutes += clampNumber(step.duration) / 60;
      return;
    }

    const volumeMl = stepVolumeMl({ ...step, volume: Number(step.volume), flow_rate: Number(step.flow_rate) });
    const flowMlpMin = stepFlowMlpMin({ ...step, flow_rate: Number(step.flow_rate) });
    if (volumeMl > 0 && flowMlpMin > 0) {
      totalMinutes += volumeMl / flowMlpMin;
      totalVolumeMl += volumeMl;
    }
  });

  const bed = bedVolume();
  const totalVolumeBv = bed > 0 ? totalVolumeMl / bed : 0;

  if (el.totalTime) {
    el.totalTime.textContent = `${totalMinutes.toFixed(1)} min`;
  }
  if (el.totalVolumeMl) {
    el.totalVolumeMl.textContent = `${totalVolumeMl.toFixed(1)} mL`;
  }
  if (el.totalVolumeBv) {
    el.totalVolumeBv.textContent = `${totalVolumeBv.toFixed(2)} BV`;
  }
}

function syncModeUi(forceRender = false) {
  const mode = modeKind();
  const modeSignature = `${state.mode}|${state.connected.pump_a}|${state.connected.pump_b}|${state.connected.valco}`;
  const modeChanged = state.lastUiMode !== modeSignature;

  const connectedPumps = [state.connected.pump_a, state.connected.pump_b].filter(Boolean).length;
  const pumpLabel = connectedPumps === 2
    ? 'Pump A + Pump B'
    : connectedPumps === 1
      ? (state.connected.pump_a ? 'Pump A' : 'Pump B')
      : '';
  if (el.modeBadge) {
    el.modeBadge.textContent = modeLabel();
  }
  el.connectionBadge.textContent = connectedPumps
    ? (mode === 'pump_only' ? `${pumpLabel} connected` : `${pumpLabel} + Valco connected`)
    : 'Disconnected';
  el.solutionsPanel.classList.add('hidden');
  el.pumpAStatusText.textContent = state.connected.pump_a ? 'Connected' : 'Disconnected';
  el.pumpBStatusText.textContent = state.connected.pump_b ? 'Connected' : 'Disconnected';
  el.valcoStatusText.textContent = state.connected.valco ? 'Connected' : 'Disconnected';
  el.pumpADot.className = `dot ${state.connected.pump_a ? 'connected' : 'disconnected'}`;
  el.pumpBDot.className = `dot ${state.connected.pump_b ? 'connected' : 'disconnected'}`;
  el.valcoDot.className = `dot ${state.connected.valco ? 'connected' : 'disconnected'}`;

  if (forceRender || !state.settingsLoaded || modeChanged) {
    renderSolutions();
    renderChannelConfig();
    renderValcoOutputs();
    renderSteps();
    state.lastUiMode = modeSignature;
  }
}

function setValveChanging(active) {
  if (!el.valveDevice) {
    return;
  }
  el.valveDevice.classList.toggle('is-changing', active);
  if (active) {
    window.clearTimeout(valveChangeTimer);
    valveChangeTimer = window.setTimeout(() => {
      el.valveDevice.classList.remove('is-changing');
      valveChangeTimer = null;
    }, 1100);
  }
}

function updateHardwareVisuals(previousValvePosition) {
  const pumpAConnected = state.connected.pump_a;
  const pumpBConnected = state.connected.pump_b;
  const valveConnected = state.connected.valco;
  const pumpAActive = pumpAConnected && state.active.pump_a;
  const pumpBActive = pumpBConnected && state.active.pump_b;

  el.pumpADevice.classList.toggle('is-connected', pumpAConnected);
  el.pumpBDevice.classList.toggle('is-connected', pumpBConnected);
  el.valveDevice.classList.toggle('is-connected', valveConnected);
  el.pumpADevice.classList.toggle('is-pumping', pumpAActive);
  el.pumpBDevice.classList.toggle('is-pumping', pumpBActive);

  el.pumpAActivityText.textContent = pumpAConnected ? (pumpAActive ? 'Pumping' : 'Idle') : 'Not connected';
  el.pumpBActivityText.textContent = pumpBConnected ? (pumpBActive ? 'Pumping' : 'Idle') : 'Not connected';
  el.valvePositionText.textContent = valveConnected && state.valcoPosition ? String(state.valcoPosition) : '--';
  el.valveActivityText.textContent = valveConnected ? 'Current valve number' : 'Not connected';

  if (state.valcoChanging || (previousValvePosition !== null && previousValvePosition !== state.valcoPosition)) {
    setValveChanging(true);
  } else if (!state.valcoChanging && !valveChangeTimer) {
    setValveChanging(false);
  }
}

function setBanner(message = '', error = '') {
  el.connectMessage.textContent = message;
  el.runMessage.textContent = message;
  el.runError.textContent = error;
}

function updateStatusFields(status) {
  const previousValvePosition = state.valcoPosition;
  state.connected.pump_a = !!status.pump_a_connected;
  state.connected.pump_b = !!status.pump_b_connected;
  state.connected.valco = !!status.valco_connected;
  state.active.pump_a = !!status.pump_a_active;
  state.active.pump_b = !!status.pump_b_active;
  state.valcoPosition = status.valco_position || null;
  state.valcoChanging = !!status.valco_changing;
  state.mode = status.mode === 'pump_only' ? 'pump_only' : 'channel_select';
  applyConnectedPreview();
  state.running = !!status.running;
  state.isPaused = !!status.is_paused;
  state.currentStepIndex = status.current_step_index;
  state.currentStepDuration = status.current_step_duration;
  state.timeRemaining = status.time_remaining;
  state.lastError = status.last_error || '';
  state.lastMessage = status.last_message || '';
  state.calibration = status.calibration || state.calibration;
  state.statusSampleAt = Date.now();

  if (el.resumeMethodBtn) {
    el.resumeMethodBtn.classList.toggle('hidden', !status.is_paused);
  }

  if (!state.settingsLoaded && status.bed_volume_ml !== undefined) {
    state.bedVolumeMl = clampNumber(status.bed_volume_ml);
    el.bedVolumeInput.value = state.bedVolumeMl;

    if (status.calibration) {
      el.calibrationSlopeInput.value = status.calibration.m;
      el.calibrationInterceptInput.value = status.calibration.b;
    }

    if (status.solutions) {
      state.solutions = Object.entries(status.solutions).map(([position, solution]) => ({
        position: Number(position),
        name: solution.name || '',
        category: solution.category || 'Other',
      })).sort((a, b) => a.position - b.position);
    }

    if (status.channels) {
      state.channels = Object.entries(status.channels).map(([channel, value]) => ({
        channel: Number(channel),
        name: value.name || '',
        category: value.category || 'Other',
      })).sort((a, b) => a.channel - b.channel);
    }

    if (status.valco_outputs) {
      state.valcoOutputs = Object.entries(status.valco_outputs).map(([position, value]) => ({
        position: Number(position),
        label: value.label || '',
      })).sort((a, b) => a.position - b.position);
    }
  }

  el.bedVolumeReadout.textContent = `${Number(state.bedVolumeMl).toFixed(1)} mL`;
  el.calibrationReadout.textContent = `y = ${Number(state.calibration.m).toFixed(3)}x + ${Number(state.calibration.b).toFixed(3)}`;
  el.runStateText.textContent = state.running ? 'Running' : 'Idle';
  el.currentStepText.textContent = status.current_step_label || 'None';
  el.timeRemainingText.textContent = typeof state.timeRemaining === 'number' ? formatTime(state.timeRemaining) : '--:--';
  el.runError.textContent = state.lastError || '';
  el.runMessage.textContent = state.lastMessage || '';

  syncModeUi();
  updateHardwareVisuals(previousValvePosition);
  renderProgressBars();

  state.settingsLoaded = true;
}

function renderProgressBars() {
  const activeStep = state.running && Number.isFinite(state.currentStepIndex)
    ? state.steps[state.currentStepIndex - 1]
    : null;
  const activeStepId = activeStep ? activeStep.id : null;
  const duration = Number(state.currentStepDuration);
  const baseRemaining = Number(state.timeRemaining);
  const elapsedSeconds = state.running && !state.isPaused && state.statusSampleAt
    ? (Date.now() - state.statusSampleAt) / 1000
    : 0;
  const remaining = Number.isFinite(baseRemaining) ? Math.max(0, baseRemaining - elapsedSeconds) : NaN;

  document.querySelectorAll('.progress-bar').forEach((bar) => {
    const width =
      activeStepId &&
      bar.dataset.stepId === activeStepId &&
      Number.isFinite(duration) &&
      duration > 0 &&
      Number.isFinite(remaining)
        ? Math.max(0, Math.min(100, (1 - remaining / duration) * 100))
        : 0;
    bar.style.width = `${width}%`;
  });
}

function formatTime(seconds) {
  if (!Number.isFinite(seconds)) {
    return '--:--';
  }
  const total = Math.max(0, Math.ceil(seconds));
  const minutes = Math.floor(total / 60);
  const remainder = total % 60;
  return `${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`;
}

function handleDragStart(event) {
  draggedStepId = event.currentTarget.dataset.stepId;
  event.currentTarget.classList.add('dragging');
  event.dataTransfer.effectAllowed = 'move';
  event.dataTransfer.setData('text/plain', draggedStepId);
}

function handleDragOver(event) {
  event.preventDefault();
  event.currentTarget.classList.add('drag-over');
  event.dataTransfer.dropEffect = 'move';
}

function handleDrop(event) {
  event.preventDefault();
  const targetId = event.currentTarget.dataset.stepId;
  const sourceId = draggedStepId || event.dataTransfer.getData('text/plain');
  draggedStepId = null;

  if (!sourceId || !targetId || sourceId === targetId) {
    return;
  }

  const fromIndex = state.steps.findIndex((step) => step.id === sourceId);
  const toIndex = state.steps.findIndex((step) => step.id === targetId);
  if (fromIndex < 0 || toIndex < 0) {
    return;
  }

  const [step] = state.steps.splice(fromIndex, 1);
  state.steps.splice(toIndex, 0, step);
  renderSteps();
}

function handleDragEnd(event) {
  event.currentTarget.classList.remove('dragging');
  document.querySelectorAll('.step-row.drag-over').forEach((row) => row.classList.remove('drag-over'));
  draggedStepId = null;
}

function handleSolutionChange(event) {
  const position = Number(event.target.dataset.position);
  const field = event.target.dataset.solutionField;
  const solution = state.solutions.find((item) => item.position === position);
  if (!solution) {
    return;
  }
  solution[field] = event.target.value;
  renderSteps();
}

function handleChannelConfigChange(event) {
  const channel = Number(event.target.dataset.channel);
  const field = event.target.dataset.channelField;
  const item = state.channels.find((entry) => entry.channel === channel);
  if (!item) {
    return;
  }
  item[field] = event.target.value;
  renderSteps();
}

function handleValcoOutputChange(event) {
  const position = Number(event.target.dataset.position);
  const field = event.target.dataset.valcoOutputField;
  const output = state.valcoOutputs.find((item) => item.position === position);
  if (!output) {
    return;
  }
  output[field] = event.target.value;
  renderSteps();
}

function handleStepChange(event) {
  const row = event.target.closest('[data-step-id]');
  const step = state.steps.find((item) => item.id === row.dataset.stepId);
  if (!step) {
    return;
  }
  const field = event.target.dataset.field;
  step[field] = event.target.value;
  if (field === 'step_type') {
  renderSteps();
    return;
  }
  if (field === 'direction') {
    step.direction = normalizeDirection(step.direction);
  }
  if (field === 'solution_position') {
    const selected = state.solutions.find((solution) => String(solution.position) === String(event.target.value));
    step.solution_name = selected ? selected.name : '';
  }
  if (field === 'primary_channel') {
    step.primary_channel = Number(event.target.value);
  }
  if (field === 'valco_output_position') {
    step.valco_output_position = Number(event.target.value);
  }
  if (field === 'flow_rate' || field === 'flow_unit') {
    const wasCapped = capStepFlowRate(step);
    if (wasCapped) {
      const flowInput = row.querySelector('[data-field="flow_rate"]');
      if (flowInput) {
        flowInput.value = String(step.flow_rate);
      }
    }
  }

  // Update RPM display for this row in-place to keep edits responsive
  const rpm = stepRpm(step);
  const rpmElem = row.querySelector('.rpm-display');
  if (rpmElem) {
    if (rpm === null) {
      rpmElem.textContent = 'RPM unavailable';
      rpmElem.classList.remove('warning');
    } else {
      rpmElem.textContent = `${rpm.toFixed(1)} RPM`;
      if (rpm > 100) rpmElem.classList.add('warning'); else rpmElem.classList.remove('warning');
    }
  }
  updateMethodTotals();
}

function handleChannelToggle(event) {
  const button = event.target.closest('[data-channel]');
  if (!button) {
    return;
  }
  const row = button.closest('[data-step-id]');
  if (!row) {
      return;
    }
  const step = state.steps.find((item) => item.id === row.dataset.stepId);
  if (!step) {
    return;
  }

  const channel = Number(button.dataset.channel);
  const selected = new Set(normalizeChannels(step.channels));
  if (selected.has(channel)) {
    selected.delete(channel);
  } else {
    selected.add(channel);
  }
  step.channels = normalizeChannels(Array.from(selected));
  // Ensure channels are in ascending numeric order after toggling
  step.channels.sort((a, b) => a - b);
  renderSteps();
}

function handleStepAction(event) {
  const row = event.target.closest('[data-step-id]');
  const index = state.steps.findIndex((item) => item.id === row.dataset.stepId);
  if (index < 0) {
    return;
  }

  const action = event.target.dataset.action;
  if (action === 'delete') {
    state.steps.splice(index, 1);
  } else if (action === 'up' && index > 0) {
    [state.steps[index - 1], state.steps[index]] = [state.steps[index], state.steps[index - 1]];
  } else if (action === 'down' && index < state.steps.length - 1) {
    [state.steps[index + 1], state.steps[index]] = [state.steps[index], state.steps[index + 1]];
  }
  renderSteps();
}

function serializeSteps() {
  return state.steps.map((step) => {
    const data = {
      id: step.id,
      step_type: step.step_type || 'flow',
      direction: normalizeDirection(step.direction),
      channels: normalizeChannels(step.channels),
      valco_output_position: step.valco_output_position || 1,
    };

    if (data.step_type === 'pause') {
      data.duration = step.duration;
    } else {
      data.volume = step.volume;
      data.volume_unit = step.volume_unit;
      data.solution_position = step.solution_position;
      data.solution_name = step.solution_name;
      data.flow_rate = step.flow_rate;
      data.flow_unit = step.flow_unit;
      data.primary_channel = step.primary_channel || 1;
    }

    return data;
  });
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || 'Request failed');
  }
  return data;
}

async function refreshPorts() {
  const data = await fetchJson('/ports', { method: 'GET', headers: {} });
  state.ports = data.ports || [];
  renderPortSelects();
}

function renderPortSelects() {
  const portOptions = state.ports.map((port) => `<option value="${escapeHtml(port.device)}">${escapeHtml(port.device)} - ${escapeHtml(port.description || '')}</option>`).join('');
  el.pumpAPortSelect.innerHTML = `<option value="">Select pump A port</option>${portOptions}`;
  el.pumpBPortSelect.innerHTML = `<option value="">Select pump B port (optional)</option>${portOptions}`;
  el.valcoPortSelect.innerHTML = `<option value="">None / Not connected</option>${portOptions}`;
}

async function connectDevices() {
  try {
    const data = await fetchJson('/connect', {
      method: 'POST',
      body: JSON.stringify({
        pump_a_port: el.pumpAPortSelect.value,
        pump_b_port: el.pumpBPortSelect.value,
        valco_port: el.valcoPortSelect.value,
      }),
    });
    if (data.warning) {
      setBanner(data.warning, '');
    } else {
      setBanner('Connected.', '');
    }
    await syncStatus();
  } catch (error) {
    setBanner('', error.message);
  }
}

async function syncStatus() {
  const response = await fetch('/status');
  const data = await response.json();
  updateStatusFields(data);
}

async function saveMethod() {
  const filename = window.prompt('Enter a filename for the method (.txt will be added automatically if needed):', 'method.txt');
  if (!filename) {
    return;
  }
  try {
    const data = await fetchJson('/save_method', {
      method: 'POST',
      body: JSON.stringify({
        filename,
        mode: state.mode,
        channel_config: Object.fromEntries(state.channels.map((channel) => [String(channel.channel), channel])),
        valco_output_config: Object.fromEntries(state.valcoOutputs.map((output) => [String(output.position), output])),
        steps: serializeSteps(),
      }),
    });
    setBanner(`Saved method to ${data.filename}.`, '');
  } catch (error) {
    setBanner('', error.message);
  }
}

async function loadMethodFromFile(file) {
  const formData = new FormData();
  formData.append('file', file);
  const response = await fetch('/load_method', {
    method: 'POST',
    body: formData,
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || 'Load failed');
  }
  state.steps = (data.steps || []).map((step) => ({
    ...stepTemplate(),
    ...step,
    id: makeId(),
    direction: normalizeDirection(step.direction),
    channels: normalizeChannels(step.channels),
    primary_channel: step.primary_channel || 1,
    valco_output_position: step.valco_output_position || 1,
  }));
  if (data.warning) {
    setBanner(data.warning, '');
  } else {
    setBanner('Method loaded.', '');
  }
  renderSteps();
  await syncStatus();
}

async function runMethod() {
  try {
    const data = await fetchJson('/run', {
      method: 'POST',
      body: JSON.stringify({
        bed_volume_ml: bedVolume(),
        calibration: {
          m: clampNumber(el.calibrationSlopeInput.value),
          b: clampNumber(el.calibrationInterceptInput.value),
        },
        mode: state.mode,
        solutions: Object.fromEntries(state.solutions.map((solution) => [String(solution.position), solution])),
        channel_config: Object.fromEntries(state.channels.map((channel) => [String(channel.channel), channel])),
        valco_output_config: Object.fromEntries(state.valcoOutputs.map((output) => [String(output.position), output])),
        steps: serializeSteps(),
      }),
    });
    setBanner(data.message || 'Run started.', '');
    await syncStatus();
  } catch (error) {
    setBanner('', error.message);
  }
}

async function stopMethod() {
  try {
    const data = await fetchJson('/stop', { method: 'POST', body: JSON.stringify({}) });
    setBanner(data.message || 'Stop requested.', '');
    await syncStatus();
  } catch (error) {
    setBanner('', error.message);
  }
}

async function resumeMethod() {
  try {
    const data = await fetchJson('/pause_ack', { method: 'POST', body: JSON.stringify({}) });
    setBanner(data.message || 'Resuming run.', '');
    await syncStatus();
  } catch (error) {
    setBanner('', error.message);
  }
}

function addStep() {
  state.steps.push(stepTemplate());
  renderSteps();
}

function installEvents() {
  el.refreshPortsBtn.addEventListener('click', refreshPorts);
  el.connectBtn.addEventListener('click', connectDevices);
  el.addStepBtn.addEventListener('click', addStep);
  el.saveMethodBtn.addEventListener('click', saveMethod);
  el.loadMethodBtn.addEventListener('click', () => el.methodFileInput.click());
  el.methodFileInput.addEventListener('change', async () => {
    const file = el.methodFileInput.files && el.methodFileInput.files[0];
    if (!file) {
      return;
    }
    try {
      await loadMethodFromFile(file);
    } catch (error) {
      setBanner('', error.message);
    } finally {
      el.methodFileInput.value = '';
    }
  });
  el.runMethodBtn.addEventListener('click', runMethod);
  el.stopMethodBtn.addEventListener('click', stopMethod);
  el.resumeMethodBtn.addEventListener('click', resumeMethod);
  el.openCalibrationBtn.addEventListener('click', () => el.conversionSettingsPanel.classList.toggle('hidden'));


  el.saveCalibrationBtn.addEventListener('click', () => {
    state.bedVolumeMl = clampNumber(el.bedVolumeInput.value);
    state.calibration = {
      m: clampNumber(el.calibrationSlopeInput.value),
      b: clampNumber(el.calibrationInterceptInput.value),
    };
    el.bedVolumeReadout.textContent = `${Number(state.bedVolumeMl).toFixed(1)} mL`;
    el.calibrationReadout.textContent = `y = ${state.calibration.m.toFixed(3)}x + ${state.calibration.b.toFixed(3)}`;
    el.conversionSettingsPanel.classList.add('hidden');
    renderSteps();
  });

  el.bedVolumeInput.addEventListener('input', () => {
    state.bedVolumeMl = clampNumber(el.bedVolumeInput.value);
    el.bedVolumeReadout.textContent = `${Number(state.bedVolumeMl).toFixed(1)} mL`;
    renderSteps();
  });
  el.calibrationSlopeInput.addEventListener('input', () => renderSteps());
  el.calibrationInterceptInput.addEventListener('input', () => renderSteps());

  el.themeSelect.addEventListener('change', (e) => {
    const theme = e.target.value;
    if (theme === 'classic') {
      document.documentElement.removeAttribute('data-theme');
    } else {
      document.documentElement.setAttribute('data-theme', theme);
    }
    localStorage.setItem('lab-theme', theme);
  });

  if (el.connectedPreviewSwitch) {
    el.connectedPreviewSwitch.addEventListener('change', async (event) => {
      state.connectedPreview = event.target.checked;
      if (state.connectedPreview) {
        state.mode = 'channel_select';
      } else {
        await syncStatus();
        setBanner('', '');
        return;
      }
      applyConnectedPreview();
      syncModeUi(true);
      updateHardwareVisuals(state.valcoPosition);
      setBanner(
        state.connectedPreview ? 'Previewing connected hardware layout.' : '',
        ''
      );
    });
  }

  if (el.openChannelsModalBtn) {
    el.openChannelsModalBtn.addEventListener('click', () => {
      el.channelsModal.classList.remove('hidden');
    });
  }
  if (el.closeChannelsModalBtn) {
    el.closeChannelsModalBtn.addEventListener('click', () => {
      el.channelsModal.classList.add('hidden');
    });
  }

  if (el.openValcoOutputsModalBtn) {
    el.openValcoOutputsModalBtn.addEventListener('click', () => {
      el.valcoOutputsModal.classList.remove('hidden');
    });
  }
  if (el.closeValcoOutputsModalBtn) {
    el.closeValcoOutputsModalBtn.addEventListener('click', () => {
      el.valcoOutputsModal.classList.add('hidden');
    });
  }

  // Delegated handlers on steps container to survive re-renders
  el.stepsContainer.addEventListener('input', (event) => {
    const target = event.target;
    if (target && target.dataset && target.dataset.field) {
      handleStepChange(event);
    }
  });

  el.stepsContainer.addEventListener('click', (event) => {
    const target = event.target;
    if (target && target.dataset && target.dataset.channel) {
      handleChannelToggle(event);
    }
    if (target && target.dataset && target.dataset.action) {
      handleStepAction(event);
    }
  });
}

function cacheElements() {
  [
    'themeSelect', 'modeBadge', 'connectionBadge', 'refreshPortsBtn', 'pumpAPortSelect', 'pumpBPortSelect', 'valcoPortSelect', 'connectBtn',
    'pumpADot', 'pumpBDot', 'valcoDot', 'pumpAStatusText', 'pumpBStatusText', 'valcoStatusText', 'connectMessage', 'bedVolumeInput',
    'bedVolumeReadout', 'calibrationReadout', 'totalTime', 'totalVolumeMl', 'totalVolumeBv', 'openCalibrationBtn', 'editCalibrationBtn', 'conversionSettingsPanel', 'calibrationPanel', 'solutionsPanel', 'solutionsGrid', 'stepsContainer',
    'saveMethodBtn', 'loadMethodBtn', 'methodFileInput', 'addStepBtn', 'methodWarning', 'runMethodBtn',
    'stopMethodBtn', 'runStateText', 'currentStepText', 'timeRemainingText', 'runError', 'runMessage',
    'saveCalibrationBtn', 'calibrationSlopeInput', 'calibrationInterceptInput',
    'cancelCalibrationBtn', 'resumeMethodBtn', 'channelsModal', 'openChannelsModalBtn', 'closeChannelsModalBtn', 'channelsGrid', 'valcoOutputsGrid',
    'openValcoOutputsModalBtn', 'closeValcoOutputsModalBtn', 'valcoOutputsModal',
    'pumpADevice', 'pumpBDevice', 'valveDevice', 'pumpAActivityText', 'pumpBActivityText', 'valveActivityText', 'valvePositionText', 'connectedPreviewSwitch'
  ].forEach((id) => {
    el[id] = document.getElementById(id);
  });
}

async function boot() {
  cacheElements();

  const savedTheme = localStorage.getItem('lab-theme');
  if (savedTheme) {
    el.themeSelect.value = savedTheme;
    if (savedTheme === 'classic') {
      document.documentElement.removeAttribute('data-theme');
    } else {
      document.documentElement.setAttribute('data-theme', savedTheme);
    }
  }

  installEvents();
  state.steps = [stepTemplate()];
  await refreshPorts();
  await syncStatus();
  renderSteps();
  renderSolutions();
  renderChannelConfig();
  renderValcoOutputs();
  setInterval(syncStatus, 650);
  setInterval(renderProgressBars, 200);
}

boot().catch((error) => {
  console.error(error);
  const banner = document.getElementById('runError');
  if (banner) {
    banner.textContent = error.message;
  }
});
