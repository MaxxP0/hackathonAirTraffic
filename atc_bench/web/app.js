(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const escape = (value) => String(value ?? '').replace(/[&<>"']/g, (char) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char]));
  const number = (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback;
  const format = (value, digits = 0) => number(value).toLocaleString('en-US', {maximumFractionDigits: digits, minimumFractionDigits: digits});
  const finished = new Set(['landed', 'departed', 'diverted', 'crashed']);
  const groundStates = new Set(['ground', 'landing_roll', 'taxi_in', 'takeoff_roll']);
  const scenarioNames = {mixed: 'Mixed traffic', rush_hour: 'Rush hour', low_visibility: 'Low visibility', storm: 'Convective weather', emergency: 'Emergency arrival', wind_shift: 'Wind shift'};
  const canvas = $('radar');
  const context = canvas.getContext('2d');
  let observation = null;
  let running = false;
  let pending = false;
  let selected = null;
  let filter = 'all';
  let range = 40;
  let playbackTimer = null;
  let width = 0;
  let height = 0;
  let hitTargets = [];
  let commandHistory = [];
  let historyIndex = -1;
  let lastEventFingerprint = '';
  let inferenceStartedAt = null;
  let inferenceTimer = null;
  let pauseRequestedDuringInference = false;
  let lastDecisionFingerprint = '';
  let statePollTimer = null;
  let statePollPending = false;
  let externalInferenceStartedAt = null;

  const controllerKind = () => observation?.controller?.kind || 'reference';
  const controllerName = () => controllerKind() === 'lmstudio' ? 'LM Studio' : 'Reference';
  const llmEnabled = () => controllerKind() === 'lmstudio' && $('autopilot').checked;
  const decisionInterval = () => number($('decision-interval').value, 120);
  const externalThinking = () => inferenceStartedAt === null && observation?.controller?.status === 'thinking';

  function clock(seconds, hours = false) {
    const value = Math.max(0, Math.floor(number(seconds)));
    const h = Math.floor(value / 3600);
    const m = Math.floor(value / 60) % 60;
    const s = value % 60;
    return `${hours ? String(h).padStart(2, '0') + ':' : ''}${String(hours ? m : Math.floor(value / 60)).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
  }

  function setNotice(message = '') {
    $('notice').textContent = message;
    $('notice').hidden = !message;
  }

  function setConnected(connected) {
    $('connection').classList.toggle('offline', !connected);
    $('connection').innerHTML = `<i></i>${connected ? 'SIM CONNECTED' : 'SIM OFFLINE'}`;
  }

  function updateControls() {
    const busy = pending || externalThinking();
    $('play-label').textContent = running ? 'Pause simulation' : observation?.done ? 'Episode complete' : 'Run simulation';
    $('play-icon').textContent = running ? 'Ⅱ' : '▶';
    $('radar-state').textContent = observation?.done ? 'COMPLETE' : inferenceStartedAt !== null ? pauseRequestedDuringInference ? 'PAUSING' : 'LLM THINKING' : externalThinking() ? 'CONTROLLER THINKING' : running ? 'RUNNING' : 'PAUSED';
    $('pulse-dot').classList.toggle('running', running);
    // Pause remains available while inference runs. The in-flight decision completes,
    // but no subsequent decision is scheduled after the user pauses.
    $('play').disabled = (busy && !running) || Boolean(observation?.done) || !observation;
    $('step').disabled = busy || Boolean(observation?.done) || !observation;
    $('reset').disabled = busy;
    $('send-command').disabled = busy || Boolean(observation?.done) || !observation;
    $('scenario').disabled = busy;
    $('seed').disabled = busy;
    $('controller-kind').disabled = busy;
    $('controller-model').disabled = busy || $('controller-kind').value !== 'lmstudio';
    $('apply-controller').disabled = busy || !observation;
    $('autopilot').disabled = busy;
    $('decision-interval').disabled = busy;
    $('speed').disabled = busy || llmEnabled();
    $('speed').title = llmEnabled() ? 'LLM mode advances the selected decision window immediately' : 'Reference playback speed';
    $('step').title = `Advance ${llmEnabled() ? decisionInterval() : 10} simulation seconds`;
    $('step').setAttribute('aria-label', $('step').title);
    syncControllerPolling();
    renderController();
  }

  function syncControllerPolling() {
    if (!externalThinking()) {
      clearTimeout(statePollTimer);
      statePollTimer = null;
      externalInferenceStartedAt = null;
      return;
    }
    if (externalInferenceStartedAt === null) externalInferenceStartedAt = performance.now();
    if (statePollTimer !== null || statePollPending) return;
    statePollTimer = setTimeout(pollControllerState, 1000);
  }

  async function pollControllerState() {
    statePollTimer = null;
    statePollPending = true;
    try {
      const state = await request('/api/state');
      acceptObservation(state, state.controller?.status !== 'thinking');
      setConnected(true);
      if (state.controller?.error) {
        pause();
        setNotice(state.controller.error);
        addConsoleLine('CONTROLLER ERROR', state.controller.error, false);
      } else setNotice();
    } catch (error) {
      pause();
      if ($('notice').textContent !== error.message) addConsoleLine('STATUS ERROR', error.message, false);
      setNotice(error.message);
      setConnected(false);
    } finally {
      statePollPending = false;
      updateControls();
    }
  }

  function pause() {
    if (inferenceStartedAt !== null) pauseRequestedDuringInference = true;
    running = false;
    clearTimeout(playbackTimer);
    playbackTimer = null;
    updateControls();
  }

  async function request(path, body) {
    const response = await fetch(path, body === undefined ? {cache: 'no-store'} : {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
    });
    let data;
    try { data = await response.json(); } catch (_) { throw new Error(`The simulator returned an invalid response (${response.status}).`); }
    if (!response.ok || data.error) {
      const error = new Error(data.error || `Simulator request failed (${response.status}).`);
      error.controller = data.controller;
      error.observation = data.observation || (data.aircraft ? data : null);
      throw error;
    }
    return data;
  }

  async function advance(seconds, commands = [], autopilot = $('autopilot').checked) {
    if (pending || externalThinking() || !observation || observation.done) return;
    pending = true;
    if (seconds > 0 && autopilot && controllerKind() === 'lmstudio') {
      inferenceStartedAt = performance.now();
      pauseRequestedDuringInference = false;
      inferenceTimer = setInterval(renderController, 250);
    }
    updateControls();
    try {
      const state = await request('/api/step', {seconds, commands, autopilot});
      acceptObservation(state);
      setConnected(true);
      setNotice();
    } catch (error) {
      if (error.observation) acceptObservation(error.observation);
      else if (error.controller) observation.controller = error.controller;
      pause();
      setNotice(error.message);
      setConnected(Boolean(error.controller));
      addConsoleLine('ERROR', error.message, false);
    } finally {
      clearInterval(inferenceTimer);
      inferenceTimer = null;
      inferenceStartedAt = null;
      pending = false;
      updateControls();
    }
  }

  function scheduleTick(immediate = false) {
    if (!running || observation?.done) return;
    const llm = llmEnabled();
    playbackTimer = setTimeout(async () => {
      await advance(llm ? decisionInterval() : number($('speed').value, 10));
      scheduleTick();
    }, immediate || llm ? 0 : 1000);
  }

  function renderController() {
    const controller = observation?.controller;
    const llm = controllerKind() === 'lmstudio';
    const thinking = inferenceStartedAt !== null || controller?.status === 'thinking';
    const status = thinking ? 'thinking' : controller?.error || controller?.status === 'error' ? 'error' : !$('autopilot').checked ? 'manual' : controller?.status || 'idle';
    $('controller-label').textContent = `${controllerName()} controller`;
    $('controller-status').textContent = status.toUpperCase();
    $('controller-status').className = `controller-status ${status}`;
    $('controller-model-name').textContent = llm ? controller?.model || 'LM Studio · automatic model selection' : 'Rule-based reference controller';
    const decision = controller?.last_decision;
    const latency = decision?.latency_s;
    $('controller-timing').textContent = thinking && inferenceStartedAt !== null ? `${format((performance.now() - inferenceStartedAt) / 1000, 1)}s elapsed · simulation waiting` : thinking && externalInferenceStartedAt !== null ? `Observing for ${format((performance.now() - externalInferenceStartedAt) / 1000, 1)}s · waiting for current decision` : `${number(controller?.decision_count)} decisions${latency != null ? ` · last call ${format(latency, 1)}s` : ''}${decision?.memory_turns != null ? ` · ${decision.memory_turns} turns in memory` : ''}`;
    if (thinking) $('controller-summary').textContent = externalThinking() ? 'A controller decision is already in progress. Waiting for its result before enabling controls.' : pauseRequestedDuringInference ? 'Pause requested. The current decision will finish; no further decisions will run.' : `Planning the next ${decisionInterval()} simulated seconds. Simulation time is paused while the model thinks.`;
    else if (controller?.error) $('controller-summary').textContent = controller.error;
    else if (decision) {
      const commands = Array.isArray(decision.commands) ? decision.commands : [];
      $('controller-summary').textContent = decision.summary || (commands.length ? commands.join(' · ') : 'No commands this decision; aircraft continue their current clearances.');
      const fingerprint = JSON.stringify([controller.kind, controller.decision_count, decision]);
      if (fingerprint !== lastDecisionFingerprint) {
        lastDecisionFingerprint = fingerprint;
        addConsoleLine(`${controllerName().toUpperCase()} #${number(controller.decision_count)}`, `${commands.length} command${commands.length === 1 ? '' : 's'}${latency != null ? ` · ${format(latency, 1)}s` : ''}${decision.summary ? ` · ${decision.summary}` : ''}`, true);
      }
    } else $('controller-summary').textContent = controller?.message || (llm ? 'Persistent conversation and plans → model commands. Start with Run or Step.' : 'The reference controller uses fixed rules. Start with Run or Step.');
    $('controller-plan').textContent = decision?.plan || 'The model’s operational plan will appear after its first decision.';
    $('decision-window-note').textContent = llm ? `${decisionInterval()} simulated seconds per decision · advances immediately after commands arrive` : 'Reference playback follows the selected speed.';
  }

  function activeAircraft() {
    return (observation?.aircraft || []).filter((aircraft) => !finished.has(aircraft.status));
  }

  function acceptObservation(state, recordCommands = true) {
    if (!state || !Array.isArray(state.aircraft) || !state.airport || !state.weather || !state.metrics) throw new Error('The simulator returned an incomplete observation.');
    observation = state;
    $('controller-kind').value = controllerKind();
    if (state.done) pause();
    if (selected && !state.aircraft.some((aircraft) => aircraft.callsign === selected)) selected = null;
    const active = activeAircraft();
    const arrivals = active.filter((aircraft) => aircraft.kind === 'arrival').length;
    const departures = active.length - arrivals;
    const metrics = state.metrics;
    $('sim-clock').textContent = `T+ ${clock(state.time_s, true)}`;
    $('metric-score').textContent = format(metrics.score);
    $('metric-active').textContent = String(active.length);
    $('active-detail').textContent = `${arrivals} inbound · ${departures} outbound`;
    $('metric-completed').innerHTML = `${format(number(metrics.landed) + number(metrics.departed))}<span class="metric-unit"> / ${format(metrics.spawned)}</span>`;
    $('completed-detail').textContent = `${format(metrics.landed)} landed · ${format(metrics.departed)} departed`;
    $('metric-separation').textContent = format(metrics.separation_losses);
    $('metric-separation').parentElement.classList.toggle('alert', number(metrics.separation_losses) > 0);
    $('separation-detail').textContent = state.conflicts?.length ? `${state.conflicts.length} active conflict${state.conflicts.length === 1 ? '' : 's'}` : 'No active conflicts';
    $('metric-delay').innerHTML = `${format(number(metrics.ground_delay_seconds) / 60, 1)}<span class="metric-unit"> min</span>`;
    $('metric-ground-score').innerHTML = metrics.ground_wait_score == null ? '—' : `${format(metrics.ground_wait_score, 1)}<span class="metric-unit"> / 100</span>`;
    $('ground-wait-detail').textContent = metrics.ground_wait_mean_seconds == null ? 'No departure queue waits yet' : `Mean ${clock(metrics.ground_wait_mean_seconds)} · max ${clock(metrics.ground_wait_max_seconds)}`;
    $('metric-emergency-score').innerHTML = metrics.emergency_wait_score == null ? '—' : `${format(metrics.emergency_wait_score, 1)}<span class="metric-unit"> / 100</span>`;
    $('emergency-wait-detail').textContent = metrics.emergency_wait_mean_seconds == null ? 'No emergencies yet' : `Mean ${clock(metrics.emergency_wait_mean_seconds)} · max ${clock(metrics.emergency_wait_max_seconds)}`;
    const outcomes = metrics.emergency_wait_by_outcome;
    $('emergency-outcomes').textContent = metrics.emergency_wait_score == null || !outcomes ? '' : `${number(outcomes.resolved?.count)} resolved · ${number(outcomes.pending?.count)} pending · ${number(outcomes.failed?.count)} failed`;
    $('metric-emergency-score').parentElement.classList.toggle('alert', number(outcomes?.failed?.count) > 0);
    $('metric-collisions').textContent = format(metrics.collisions);
    $('metric-collisions').parentElement.classList.toggle('alert', number(metrics.collisions) > 0);
    $('collision-detail').innerHTML = `<i class="status-dot"></i>${number(metrics.collisions) ? 'Collision recorded' : 'No collisions recorded'}`;
    $('traffic-count').textContent = String(active.length);
    $('all-count').textContent = String(active.length);
    $('arrival-count').textContent = String(arrivals);
    $('departure-count').textContent = String(departures);
    $('episode-footer').textContent = `SEED ${state.seed} · ${String(state.scenario).replaceAll('_', ' ').toUpperCase()}`;
    $('empty-radar').hidden = active.length > 0;
    renderWeather();
    renderRunways();
    renderFlightList();
    renderAircraftCard();
    renderEvents();
    if (recordCommands) renderResults();
    drawRadar();
    updateControls();
  }

  function renderWeather() {
    const weather = observation.weather;
    const direction = String(Math.round(number(weather.wind_from_deg))).padStart(3, '0');
    const visibility = number(weather.visibility_m) >= 10000 ? '10+ km' : `${format(number(weather.visibility_m) / 1000, 1)} km`;
    $('wind-label').textContent = `${direction}° / ${format(weather.wind_speed_kt)} kt`;
    $('visibility-label').textContent = `VIS ${visibility.toUpperCase()} · CEILING ${format(weather.ceiling_ft)} FT`;
    $('weather-description').textContent = weather.description || 'Current terminal-area conditions';
    $('weather-wind').textContent = `${direction}° ${format(weather.wind_speed_kt)}G${format(weather.gust_kt)} kt`;
    $('weather-visibility').textContent = visibility;
    $('weather-ceiling').textContent = `${format(weather.ceiling_ft)} ft`;
    $('weather-flow').textContent = `${weather.active_direction} operations`;
    $('weather-tag').textContent = weather.cells?.length ? 'CONVECTIVE' : number(weather.visibility_m) < 5000 ? 'LOW VIS' : 'FAIR';
    $('map-weather').querySelector('.weather-icon').style.transform = `rotate(${number(weather.wind_from_deg) - 315}deg)`;
  }

  function currentRunways() {
    return (observation?.airport.runways || []).filter((runway) => String(runway.id).startsWith(observation.weather.active_direction) || runway.id === '18');
  }

  function renderRunways() {
    $('runway-strip').innerHTML = '<span class="runway-strip-label">RUNWAYS</span>' + currentRunways().map((runway) => {
      const busy = runway.occupied_by || number(runway.available_in_s) > 0;
      const status = runway.closed ? 'CLOSED' : runway.occupied_by ? runway.occupied_by : busy ? `${Math.ceil(number(runway.available_in_s))}s WAKE` : 'OPEN';
      return `<span class="runway-status ${busy ? 'busy' : ''} ${runway.closed ? 'closed' : ''}" title="${escape(runway.length_m)} m · ${runway.arrival ? 'Arrival' : ''}${runway.arrival && runway.departure ? ' / ' : ''}${runway.departure ? 'Departure' : ''}"><i></i>${escape(runway.id)} <small>${escape(status)}</small></span>`;
    }).join('');
  }

  function flightStatus(aircraft) {
    if (aircraft.emergency) return `⚠ ${String(aircraft.emergency.kind).replaceAll('_', ' ')}`;
    if (aircraft.runway && ['approach', 'landing_roll', 'takeoff_roll'].includes(aircraft.status)) return `${String(aircraft.status).replaceAll('_', ' ')} · RWY ${aircraft.runway}`;
    return String(aircraft.status).replaceAll('_', ' ');
  }

  function renderFlightList() {
    const flights = activeAircraft().filter((aircraft) => filter === 'all' || aircraft.kind === filter);
    flights.sort((a, b) => Number(Boolean(b.emergency)) - Number(Boolean(a.emergency)) || number(a.spawn_time_s) - number(b.spawn_time_s) || a.callsign.localeCompare(b.callsign));
    if (!flights.length) {
      $('flight-list').innerHTML = `<div class="empty-list">${observation?.done ? 'Episode complete.' : 'No active ' + (filter === 'all' ? 'flights' : filter === 'arrival' ? 'arrivals' : 'departures') + '.'}<br>${observation?.done ? 'Start a new episode to run another scenario.' : 'Advance the simulation to receive more traffic.'}</div>`;
      return;
    }
    $('flight-list').innerHTML = flights.map((aircraft) => `<button class="flight-row ${escape(aircraft.kind)} ${aircraft.emergency ? 'emergency' : ''} ${selected === aircraft.callsign ? 'selected' : ''}" data-callsign="${escape(aircraft.callsign)}" aria-pressed="${selected === aircraft.callsign}"><span class="flight-symbol">${aircraft.kind === 'arrival' ? '↙' : '↗'}</span><span class="flight-main"><span class="flight-top"><span class="flight-callsign">${escape(aircraft.callsign)}</span><span class="flight-type">${escape(aircraft.type)}</span></span><span class="flight-state">${escape(flightStatus(aircraft))}</span></span><span class="flight-telemetry">${groundStates.has(aircraft.status) ? 'GND' : format(aircraft.altitude_ft)} <span class="unit">${groundStates.has(aircraft.status) ? '' : 'ft'}</span><small>${format(aircraft.speed_kt)} kt <span class="unit">/</span> ${String(Math.round(number(aircraft.heading_deg))).padStart(3, '0')}°</small></span></button>`).join('');
  }

  function selectAircraft(callsign) {
    selected = callsign === selected ? null : callsign;
    renderFlightList();
    renderAircraftCard();
    drawRadar();
  }

  function renderAircraftCard() {
    const aircraft = observation?.aircraft.find((item) => item.callsign === selected);
    $('aircraft-card').hidden = !aircraft;
    if (!aircraft) return;
    const deadline = aircraft.emergency ? Math.max(0, number(aircraft.emergency.deadline_s) - number(observation.time_s)) : 0;
    $('aircraft-card').innerHTML = `<div class="aircraft-card-top"><strong>${escape(aircraft.callsign)}</strong><button class="close-card" aria-label="Close aircraft details">×</button></div><div class="card-sub">${escape(aircraft.type)} · ${escape(aircraft.wake)} WAKE · ${escape(aircraft.kind).toUpperCase()}</div><div class="card-data"><div><span>ALTITUDE</span><b>${format(aircraft.altitude_ft)} ft</b></div><div><span>HEADING</span><b>${format(aircraft.heading_deg)}°</b></div><div><span>SPEED</span><b>${format(aircraft.speed_kt)} kt</b></div><div><span>FUEL</span><b>${clock(aircraft.fuel_s)}</b></div><div><span>TARGET ALT</span><b>${format(aircraft.target_altitude_ft)} ft</b></div><div><span>RUNWAY</span><b>${escape(aircraft.runway || 'Unassigned')}</b></div><div><span>GROUND WAIT</span><b>${aircraft.ground_wait_s == null ? '—' : clock(aircraft.ground_wait_s)}</b></div><div><span>EMERGENCY WAIT</span><b>${aircraft.emergency_wait_s == null ? '—' : clock(aircraft.emergency_wait_s)}</b></div></div>${aircraft.emergency ? `<div class="card-emergency">⚠ ${escape(String(aircraft.emergency.kind).replaceAll('_', ' '))} · ${clock(deadline)} remaining</div>` : ''}<button class="card-command">Issue command to ${escape(aircraft.callsign)} ↗</button>`;
  }

  function addConsoleLine(command, message, accepted = true) {
    const row = document.createElement('div');
    row.className = 'console-line';
    row.innerHTML = `<span class="console-time">${clock(observation?.time_s || 0)}</span><span><span class="${accepted ? 'accepted' : 'rejected'}">${accepted ? '✓' : '×'}</span> <span class="command-text">${escape(command)}</span> <span class="result-text">${escape(message)}</span></span>`;
    $('command-history').append(row);
    while ($('command-history').children.length > 100) $('command-history').firstElementChild.remove();
    $('command-history').scrollTop = $('command-history').scrollHeight;
  }

  function renderResults() {
    const results = observation.command_results || [];
    for (const result of results) {
      const command = typeof result.command === 'string' ? result.command : JSON.stringify(result.command);
      addConsoleLine(command, result.message, result.accepted);
    }
  }

  function renderEvents() {
    const events = observation.events || [];
    const fingerprint = JSON.stringify(events);
    if (fingerprint === lastEventFingerprint) return;
    lastEventFingerprint = fingerprint;
    $('event-count').textContent = `${events.length}${events.length === 100 ? '+' : ''} EVENTS`;
    $('event-list').innerHTML = events.length ? [...events].reverse().map((event) => {
      const type = String(event.type || '');
      const severity = /collision|crash|failed|incursion|separation/.test(type) ? 'danger' : /emergency|weather|divert|wake|go_around/.test(type) ? 'warning' : '';
      return `<div class="event-row ${severity}"><span class="event-time">${clock(event.time_s)}</span><i class="event-dot"></i><span class="event-text">${escape(event.message)}</span></div>`;
    }).join('') : '<div class="empty-events">Episode events will appear here.</div>';
  }

  function project(x, y) {
    const scale = (Math.min(width, height) - 44) / (2 * range);
    return [width / 2 + number(x) * scale, height / 2 - number(y) * scale];
  }

  function radiusPx(nm) { return number(nm) * (Math.min(width, height) - 44) / (2 * range); }
  function line(points, color, lineWidth = 1, dash = []) {
    if (!points.length) return;
    context.beginPath(); context.setLineDash(dash); context.strokeStyle = color; context.lineWidth = lineWidth;
    points.forEach((point, index) => index ? context.lineTo(...point) : context.moveTo(...point));
    context.stroke(); context.setLineDash([]);
  }
  function label(text, x, y, color = '#6d8c9c', font = '9px "IBM Plex Mono", monospace') {
    context.font = font; context.fillStyle = color; context.fillText(String(text), x, y);
  }

  function drawBackground() {
    context.fillStyle = '#101920'; context.fillRect(0, 0, width, height);
    const [cx, cy] = project(0, 0);
    const grid = range <= 10 ? 2 : 10;
    const xExtent = range * width / Math.max(1, height) + grid;
    for (let x = -Math.ceil(xExtent / grid) * grid; x <= xExtent; x += grid) line([project(x, -range * 2), project(x, range * 2)], '#1b2a34', .65);
    for (let y = -range * 2; y <= range * 2; y += grid) line([project(-xExtent, y), project(xExtent, y)], '#1b2a34', .65);
    const ringStep = range <= 10 ? 2 : 10;
    for (let radius = ringStep; radius <= range; radius += ringStep) {
      context.beginPath(); context.arc(cx, cy, radiusPx(radius), 0, Math.PI * 2);
      context.strokeStyle = radius === range ? '#304753' : '#263b48'; context.lineWidth = .8; context.setLineDash(radius === range ? [3, 5] : []); context.stroke(); context.setLineDash([]);
      label(`${radius} NM`, cx + radiusPx(radius) * .7 + 5, cy - radiusPx(radius) * .7 - 4, '#415f71', '8px "IBM Plex Mono", monospace');
    }
    for (let angle = 0; angle < 360; angle += 30) {
      const rad = angle * Math.PI / 180;
      line([[cx + radiusPx(range - 1) * Math.sin(rad), cy - radiusPx(range - 1) * Math.cos(rad)], [cx + radiusPx(range) * Math.sin(rad), cy - radiusPx(range) * Math.cos(rad)]], '#506673', .8);
    }
    line([[cx - 7, cy], [cx + 7, cy]], '#55717e', .8); line([[cx, cy - 7], [cx, cy + 7]], '#55717e', .8);
  }

  function drawWeather() {
    for (const cell of observation.weather.cells || []) {
      const [x, y] = project(cell.x_nm, cell.y_nm);
      const radius = radiusPx(cell.radius_nm);
      context.save();
      context.beginPath(); context.arc(x, y, radius, 0, Math.PI * 2); context.fillStyle = '#80613020'; context.fill();
      context.strokeStyle = '#a7834e75'; context.lineWidth = 1; context.setLineDash([4, 4]); context.stroke(); context.setLineDash([]);
      context.clip();
      for (let offset = -radius * 2; offset < radius * 2; offset += 10) line([[x - radius + offset, y - radius], [x + radius + offset, y + radius]], '#a7834e22', 1);
      context.restore();
      label(`WX ${cell.id}`, x - 18, y - 3, '#bb9967', '8px "IBM Plex Mono", monospace');
      label(`${String(cell.severity).toUpperCase()}`, x - 18, y + 10, '#8f7956', '7px "IBM Plex Mono", monospace');
    }
  }

  function drawFixes() {
    for (const [name, coordinates] of Object.entries(observation.airport.fixes || {})) {
      const [x, y] = project(...coordinates);
      context.beginPath(); context.moveTo(x, y - 4); context.lineTo(x + 3.5, y + 3); context.lineTo(x - 3.5, y + 3); context.closePath(); context.strokeStyle = '#4b6776'; context.lineWidth = .8; context.stroke();
      label(name, x + 7, y + 3, '#526e7e', '8px "IBM Plex Mono", monospace');
    }
  }

  function drawRunways() {
    const runways = currentRunways();
    for (const runway of runways) {
      const start = project(...runway.threshold);
      const end = project(...runway.end);
      const color = runway.closed ? '#5b4f51' : runway.occupied_by ? '#d7a35f' : '#c1d0d5';
      if (runway.arrival && runway.approach_fix) line([start, project(...runway.approach_fix)], '#8eb2bb38', .8, [3, 4]);
      line([start, end], '#0b1216', Math.max(4, radiusPx(.18)));
      line([start, end], color, Math.max(1.5, radiusPx(.10)));
      if (range <= 20) label(runway.id, start[0] + 5, start[1] - 6, color, '8px "IBM Plex Mono", monospace');
    }
    const [cx, cy] = project(0, 0);
    if (range > 20) label('EDDF', cx - 10, cy + 28, '#a4bdc7', '9px "IBM Plex Mono", monospace');
    drawAirportInset(runways);
  }

  function drawAirportInset(runways) {
    if (width < 450 || selected) return;
    const left = 20, top = height - 157, boxWidth = 147, boxHeight = 105;
    context.fillStyle = '#121e27e6'; context.fillRect(left, top, boxWidth, boxHeight); context.strokeStyle = '#2b3e4b'; context.lineWidth = .8; context.strokeRect(left + .5, top + .5, boxWidth, boxHeight);
    label('AIRPORT DETAIL', left + 10, top + 16, '#698797', '7px "IBM Plex Mono", monospace');
    const coordinates = runways.flatMap((runway) => [runway.threshold, runway.end]);
    if (!coordinates.length) return;
    const xs = coordinates.map((point) => number(point[0])); const ys = coordinates.map((point) => number(point[1]));
    const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
    const scale = Math.min((boxWidth - 43) / Math.max(1, maxX - minX), (boxHeight - 44) / Math.max(1, maxY - minY));
    const map = (point) => [left + boxWidth / 2 + (number(point[0]) - (minX + maxX) / 2) * scale, top + 60 - (number(point[1]) - (minY + maxY) / 2) * scale];
    for (const runway of runways) {
      const start = map(runway.threshold), end = map(runway.end);
      const color = runway.closed ? '#665558' : runway.occupied_by ? '#d3a773' : '#8babb5';
      line([start, end], color, 2);
      label(runway.id, start[0] + 4, start[1] - 5, color, '7px "IBM Plex Mono", monospace');
    }
  }

  function drawConflicts() {
    for (const conflict of observation.conflicts || []) {
      const aircraft = conflict.callsigns.map((callsign) => observation.aircraft.find((item) => item.callsign === callsign)).filter(Boolean);
      if (aircraft.length !== 2) continue;
      const points = aircraft.map((item) => project(item.x_nm, item.y_nm));
      line(points, '#ff76788c', 1, [4, 3]);
      for (const point of points) { context.beginPath(); context.arc(...point, 17, 0, Math.PI * 2); context.strokeStyle = '#ff767899'; context.lineWidth = 1; context.stroke(); }
      label(`${number(conflict.distance_nm).toFixed(1)} NM`, (points[0][0] + points[1][0]) / 2 + 5, (points[0][1] + points[1][1]) / 2 - 5, '#ff9292', '8px "IBM Plex Mono", monospace');
    }
  }

  function drawAircraft() {
    const flights = activeAircraft();
    flights.sort((a, b) => Number(a.callsign === selected) - Number(b.callsign === selected));
    hitTargets = [];
    const groundSlots = new Map();
    for (const aircraft of flights) {
      let [x, y] = project(aircraft.x_nm, aircraft.y_nm);
      const isGrounded = groundStates.has(aircraft.status);
      // Separate labels for co-located aircraft in the abstract departure queue.
      let labelOffset = 0;
      if (isGrounded) {
        const key = `${Math.round(x / 15)}:${Math.round(y / 15)}`;
        labelOffset = groundSlots.get(key) || 0;
        groundSlots.set(key, labelOffset + 1);
      }
      const isSelected = aircraft.callsign === selected;
      const color = aircraft.emergency ? '#fba65e' : aircraft.kind === 'departure' ? '#84afff' : '#63dfc8';
      const history = aircraft.history || [];
      if (!isGrounded && history.length > 1) {
        line(history.slice(-35).map((point) => project(...point)), `${color}35`, 1);
        history.slice(-24).filter((_, index) => index % 4 === 0).forEach((point) => { const p = project(...point); context.fillStyle = `${color}65`; context.fillRect(p[0] - 1, p[1] - 1, 2, 2); });
      }
      const radians = number(aircraft.heading_deg) * Math.PI / 180;
      if (!isGrounded) {
        const vector = radiusPx(number(aircraft.speed_kt) / 60 * 1.5);
        line([[x, y], [x + Math.sin(radians) * vector, y - Math.cos(radians) * vector]], `${color}66`, .8);
      }
      if (isSelected) {
        context.beginPath(); context.arc(x, y, 14, 0, Math.PI * 2); context.strokeStyle = `${color}99`; context.lineWidth = 1; context.stroke();
        if (aircraft.target_heading_deg !== undefined && !isGrounded) {
          const target = number(aircraft.target_heading_deg) * Math.PI / 180;
          line([[x, y], [x + Math.sin(target) * 42, y - Math.cos(target) * 42]], `${color}88`, .8, [3, 3]);
        }
      }
      context.save(); context.translate(x, y); context.rotate(radians); context.fillStyle = color;
      if (isGrounded) { context.fillRect(-2.5, -2.5, 5, 5); }
      else { context.beginPath(); context.moveTo(0, -6); context.lineTo(4, 4); context.lineTo(0, 2); context.lineTo(-4, 4); context.closePath(); context.fill(); }
      context.restore();
      const toLeft = x > width - 118;
      const labelX = x + (toLeft ? -104 : 14);
      const labelY = y - 11 + (isGrounded ? labelOffset * 29 : 0);
      if (isGrounded && labelOffset > 0) line([[x + 4, y + 4], [labelX - 3, labelY + 1]], `${color}35`, .7);
      const textWidth = Math.max(80, aircraft.callsign.length * 7 + 5);
      context.fillStyle = isSelected ? '#182b30ed' : '#101920b8'; context.fillRect(labelX - 3, labelY - 10, textWidth, 27);
      label(aircraft.callsign, labelX, labelY, color, `${isSelected ? '500 ' : ''}10px "IBM Plex Mono", monospace`);
      const tag = isGrounded ? `${String(aircraft.status).replaceAll('_', ' ').toUpperCase()}` : `${String(Math.round(number(aircraft.altitude_ft) / 100)).padStart(3, '0')}  ${Math.round(number(aircraft.speed_kt))}KT`;
      label(tag, labelX, labelY + 12, `${color}a6`, '8px "IBM Plex Mono", monospace');
      if (aircraft.emergency) { context.fillStyle = color; context.fillRect(labelX - 3, labelY - 19, 34, 5); }
      hitTargets.push({callsign: aircraft.callsign, x, y, left: labelX - 4, top: labelY - 12, right: labelX + textWidth, bottom: labelY + 17});
    }
  }

  function drawRadar() {
    if (!width || !height) return;
    drawBackground();
    if (observation) { drawWeather(); drawFixes(); drawRunways(); drawConflicts(); drawAircraft(); }
    $('range-label').textContent = `${range} NM RANGE`;
  }

  function resizeRadar() {
    const bounds = canvas.getBoundingClientRect();
    width = bounds.width; height = bounds.height;
    const ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio);
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    drawRadar();
  }

  function zoom(delta) {
    const ranges = [5, 10, 20, 30, 40, 50, 60];
    const index = ranges.indexOf(range);
    range = ranges[Math.max(0, Math.min(ranges.length - 1, index + delta))];
    drawRadar();
  }

  function template(action) {
    const aircraft = observation?.aircraft.find((item) => item.callsign === selected);
    const callsign = aircraft?.callsign || activeAircraft().find((item) => action === 'TAKEOFF' ? item.status === 'ground' : item.kind === 'arrival')?.callsign || 'DLH101';
    const direction = observation?.weather.active_direction || '25';
    const suffixes = {HEADING: ` ${Math.round(number(aircraft?.target_heading_deg, 250))}`, ALTITUDE: ' 5000', SPEED: ' 210', APPROACH: ` ${direction}R`, TAKEOFF: ` ${direction}C`, DIRECT: ' NORTH', HOLD: '', GO_AROUND: '', DIVERT: ''};
    $('command').value = `${action} ${callsign}${suffixes[action] || ''}`;
    $('command').focus();
  }

  $('play').addEventListener('click', () => { if (running) pause(); else { running = true; updateControls(); scheduleTick(true); } });
  $('step').addEventListener('click', () => { pause(); advance(llmEnabled() ? decisionInterval() : 10); });
  $('decision-interval').addEventListener('change', updateControls);
  $('controller-kind').addEventListener('change', () => { pause(); updateControls(); });
  $('apply-controller').addEventListener('click', async () => {
    if (pending || externalThinking() || !observation) return;
    pause(); pending = true; updateControls();
    try {
      const state = await request('/api/controller', {kind: $('controller-kind').value, base_url: observation.controller?.base_url || 'http://127.0.0.1:1234', model: $('controller-model').value.trim()});
      acceptObservation(state);
      addConsoleLine('CONTROLLER', `${controllerName()} selected${state.controller?.model ? ` · ${state.controller.model}` : ''}. Press Run or Step to continue.`, true);
      setNotice(); setConnected(true);
    } catch (error) {
      if (error.controller) observation.controller = error.controller;
      $('controller-kind').value = controllerKind();
      setNotice(error.message);
      addConsoleLine('CONTROLLER ERROR', error.message, false);
    } finally { pending = false; updateControls(); }
  });
  $('reset').addEventListener('click', async () => {
    if (pending || externalThinking()) return;
    const seedText = $('seed').value;
    const seed = Number(seedText);
    if (!seedText.trim() || !Number.isSafeInteger(seed)) { setNotice('Enter an integer seed to start a reproducible episode.'); $('seed').focus(); return; }
    pause(); pending = true; updateControls();
    try {
      const state = await request('/api/reset', {seed, scenario: $('scenario').value, duration_s: observation?.duration_s || 1800});
      selected = null; lastEventFingerprint = ''; lastDecisionFingerprint = '';
      $('command-history').innerHTML = '';
      acceptObservation(state);
      addConsoleLine('EPISODE READY', `${scenarioNames[state.scenario] || state.scenario} · seed ${state.seed} · paused`, true);
      setNotice(); setConnected(true);
    } catch (error) {
      if (error.controller) observation.controller = error.controller;
      setNotice(error.message); setConnected(Boolean(error.controller));
    }
    finally { pending = false; updateControls(); }
  });
  $('command-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const input = $('command').value.trim();
    if (!input || pending || externalThinking() || !observation || observation.done) return;
    const commands = input.split(/[;\n]/).map((command) => command.trim()).filter(Boolean);
    commandHistory.push(input); historyIndex = commandHistory.length;
    $('command').value = '';
    // Manual commands never also trigger automatic control in the same zero-time step.
    await advance(0, commands, false);
    $('command').focus();
  });
  $('command').addEventListener('keydown', (event) => {
    if (!['ArrowUp', 'ArrowDown'].includes(event.key) || !commandHistory.length) return;
    event.preventDefault(); historyIndex = Math.max(0, Math.min(commandHistory.length, historyIndex + (event.key === 'ArrowUp' ? -1 : 1)));
    $('command').value = commandHistory[historyIndex] || '';
  });
  $('autopilot').addEventListener('change', () => {
    pause();
    addConsoleLine('CONTROLLER', $('autopilot').checked ? `${controllerName()} controller enabled for subsequent steps.` : 'Manual control enabled. Automatic commands disabled.', true);
  });
  document.querySelectorAll('[data-filter]').forEach((button) => button.addEventListener('click', () => {
    filter = button.dataset.filter;
    document.querySelectorAll('[data-filter]').forEach((tab) => { tab.classList.toggle('active', tab === button); tab.setAttribute('aria-selected', String(tab === button)); });
    renderFlightList();
  }));
  document.querySelectorAll('[data-template]').forEach((button) => button.addEventListener('click', () => template(button.dataset.template)));
  $('flight-list').addEventListener('click', (event) => { const row = event.target.closest('[data-callsign]'); if (row) selectAircraft(row.dataset.callsign); });
  $('clear-selection').addEventListener('click', () => { selected = null; renderAircraftCard(); renderFlightList(); drawRadar(); });
  $('aircraft-card').addEventListener('click', (event) => {
    if (event.target.closest('.close-card')) selectAircraft(selected);
    else if (event.target.closest('.card-command')) {
      const aircraft = observation.aircraft.find((item) => item.callsign === selected);
      template(aircraft?.status === 'ground' ? 'TAKEOFF' : 'HEADING');
    }
  });
  canvas.addEventListener('click', (event) => {
    const bounds = canvas.getBoundingClientRect();
    const x = event.clientX - bounds.left, y = event.clientY - bounds.top;
    const target = [...hitTargets].reverse().find((item) => Math.hypot(item.x - x, item.y - y) < 13 || x >= item.left && x <= item.right && y >= item.top && y <= item.bottom);
    if (target) selectAircraft(target.callsign);
  });
  canvas.addEventListener('keydown', (event) => {
    if (event.key === '+' || event.key === '=') { event.preventDefault(); zoom(-1); }
    else if (event.key === '-') { event.preventDefault(); zoom(1); }
    else if (event.key === 'Escape') { selected = null; renderFlightList(); renderAircraftCard(); drawRadar(); }
  });
  $('zoom-in').addEventListener('click', () => zoom(-1));
  $('zoom-out').addEventListener('click', () => zoom(1));
  $('reset-view').addEventListener('click', () => { range = 40; drawRadar(); });
  $('help-button').addEventListener('click', () => $('reference-dialog').showModal());
  $('reference-button').addEventListener('click', () => $('reference-dialog').showModal());
  $('close-reference').addEventListener('click', () => $('reference-dialog').close());
  $('reference-dialog').addEventListener('click', (event) => { if (event.target === $('reference-dialog')) { const rect = event.target.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) event.target.close(); } });
  new ResizeObserver(resizeRadar).observe($('radar-container'));
  window.addEventListener('beforeunload', () => { clearTimeout(playbackTimer); clearInterval(inferenceTimer); clearTimeout(statePollTimer); });
  if (document.fonts?.ready) document.fonts.ready.then(drawRadar);

  async function initialize() {
    updateControls();
    try {
      const state = await request('/api/state');
      $('seed').value = state.seed;
      if (![...$('scenario').options].some((option) => option.value === state.scenario)) {
        const option = new Option(scenarioNames[state.scenario] || state.scenario, state.scenario); $('scenario').add(option);
      }
      $('scenario').value = state.scenario;
      $('controller-model').value = state.controller?.model || '';
      acceptObservation(state); setConnected(true);
      addConsoleLine('CONNECTED', `EDDF · ${Math.round(number(state.duration_s) / 60)} minute episode · ${controllerName()} controller ${$('autopilot').checked ? 'on' : 'off'}`, true);
    } catch (error) {
      setConnected(false); setNotice(`${error.message} Start the local simulator with python -m atc_bench serve, then reload this page.`);
      $('flight-list').innerHTML = '<div class="empty-list">Simulator unavailable.<br>Start the local server and reload.</div>';
    } finally { updateControls(); }
  }
  initialize();
})();
