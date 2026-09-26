'use strict';

(() => {
  const $ = id => document.getElementById(id);
  const storageKey = 'vector-human-session-v1';
  let state = null;
  let busy = false;
  const num = (value, digits = 0) => typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('en-US', {maximumFractionDigits: digits, minimumFractionDigits: digits}) : '—';
  const minutes = value => typeof value === 'number' && Number.isFinite(value) ? `${num(value / 60, 1)} min` : '—';
  const clock = value => `${String(Math.floor((value || 0) / 60)).padStart(2, '0')}:${String(Math.floor((value || 0) % 60)).padStart(2, '0')}`;
  const json = value => JSON.stringify(value, null, 2);
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const savedId = () => { try { return sessionStorage.getItem(storageKey); } catch { return null; } };
  const saveId = id => { try { id ? sessionStorage.setItem(storageKey, id) : sessionStorage.removeItem(storageKey); } catch { /* Session still works without storage. */ } };

  function notice(message) {
    $('notice').textContent = message || '';
    $('notice').hidden = !message;
  }

  function setBusy(value) {
    busy = value;
    for (const id of ['start', 'scenario', 'seed', 'plan', 'commands', 'summary', 'submit-decision', 'command-flight', 'command-action', 'command-value', 'add-command']) {
      $(id).disabled = value || (!['start', 'scenario', 'seed'].includes(id) && (!state || state.done));
    }
    for (const button of $('command-queue').querySelectorAll('button')) button.disabled = value || !state || state.done;
    if (state && !(state.model_observation.aircraft || []).length) $('add-command').disabled = true;
    $('submit-decision').textContent = value ? 'Advancing simulation…' : 'Apply decision & advance 2 min ↗';
    if (state) $('sim-status').textContent = value ? 'APPLYING DECISION' : state.done ? 'EPISODE COMPLETE' : 'PAUSED · YOUR TURN';
  }

  async function request(path, payload) {
    const response = await fetch(path, payload === undefined ? {cache: 'no-store'} : {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: json(payload)
    });
    let result;
    try { result = await response.json(); } catch { throw new Error(`The simulator returned an unreadable response (${response.status}).`); }
    if (!response.ok) {
      const error = new Error(typeof result.error === 'string' ? result.error : `The simulator rejected this request (${response.status}).`);
      error.status = response.status;
      throw error;
    }
    if (!result.id || !result.model_observation || !result.observation) throw new Error('The simulator returned an incomplete controller session.');
    return result;
  }

  function cell(row, text, small, className) {
    const td = node('td', undefined, className);
    td.append(node('span', text));
    if (small) td.append(node('small', small));
    row.append(td);
    return td;
  }

  function renderObservation(model) {
    const weather = model.weather || {};
    $('weather').replaceChildren(node('p', weather.description || 'Current observed weather'));
    for (const [label, value] of [
      ['Wind', `${num(weather.wind_from_deg)}° / ${num(weather.wind_speed_kt)} kt`],
      ['Gust', `${num(weather.gust_kt)} kt`], ['Visibility', `${num(weather.visibility_m)} m`],
      ['Ceiling', `${num(weather.ceiling_ft)} ft`], ['Active flow', weather.active_direction || '—']
    ]) {
      const block = node('div');
      block.append(node('span', `${label}  `), node('strong', value));
      $('weather').append(block);
    }
    $('runways').replaceChildren();
    for (const geometry of model.airport?.runways || []) {
      const runway = {...geometry, ...model.runway_state?.[geometry.id]};
      const row = node('tr');
      cell(row, runway.id, runway.physical_id);
      cell(row, runway.arrival && runway.departure ? 'Arrival / departure' : runway.arrival ? 'Arrival only' : 'Departure only');
      cell(row, `${num(runway.length_m)} m`, `${num(runway.heading_deg)}°`);
      cell(row, runway.closed ? 'CLOSED' : runway.occupied_by || 'Open', undefined, runway.closed ? 'bad' : runway.occupied_by ? 'warning' : 'good');
      cell(row, `${num(runway.available_in_s)} s`);
      $('runways').append(row);
    }
    $('aircraft').replaceChildren();
    $('aircraft-count').textContent = `· ${(model.aircraft || []).length}`;
    for (const values of model.aircraft || []) {
      const aircraft = Object.fromEntries((model.aircraft_columns || []).map((key, index) => [key, values[index]]));
      const row = node('tr');
      const details = node('details', undefined, 'flight-details');
      const title = node('summary', aircraft.callsign);
      details.append(title, node('pre', json({...aircraft, performance_limits: model.aircraft_types?.[aircraft.type]})));
      const first = node('td');
      first.append(details, node('small', `${aircraft.type} · ${aircraft.kind}`));
      row.append(first);
      cell(row, (aircraft.status || '').replaceAll('_', ' '), `Position ${num(aircraft.x_nm, 1)}, ${num(aircraft.y_nm, 1)} NM`);
      cell(row, `${num(aircraft.altitude_ft)} ft / ${num(aircraft.speed_kt)} kt`, `Heading ${num(aircraft.heading_deg)}°`);
      cell(row, aircraft.runway || '—', aircraft.approach_stage || undefined);
      const emergency = aircraft.emergency;
      cell(row, `${minutes(aircraft.fuel_s)} fuel`, emergency ? `${emergency.type || emergency.kind || 'Emergency'} · ${typeof emergency.deadline_s === 'number' ? `${clock(Math.max(0, emergency.deadline_s - model.time_s))} to deadline` : 'active'}` : undefined, emergency ? 'warning' : undefined);
      $('aircraft').append(row);
    }
    if (!(model.aircraft || []).length) {
      const row = node('tr');
      const td = cell(row, 'No active aircraft in this observation.');
      td.colSpan = 5;
      $('aircraft').append(row);
    }
    $('conflicts').textContent = json({conflicts: model.conflicts || [], prior_command_errors: model.prior_command_errors || []});
    $('raw-observation').textContent = json(model);
  }

  function renderMetrics() {
    const metrics = state.observation.metrics || {};
    const landing = state.landing_metrics || {};
    $('score').textContent = num(metrics.score, 1);
    $('score-note').textContent = state.done ? 'Final human score · higher is better' : 'Running human score · not final';
    $('completed').textContent = `${(metrics.landed || 0) + (metrics.departed || 0)} / ${metrics.spawned || 0}`;
    $('completed-note').textContent = `${metrics.landed || 0} landed · ${metrics.departed || 0} departed · ${metrics.unfinished || 0} unfinished`;
    $('collisions').textContent = `${num(metrics.collisions)} collisions`;
    $('collisions').classList.toggle('bad', (metrics.collisions || 0) > 0);
    $('separation').textContent = `${num(metrics.separation_losses)} separation losses · ${num(metrics.separation_loss_seconds)} pair-seconds`;
    $('ground').textContent = minutes(metrics.ground_wait_mean_seconds);
    $('ground-note').textContent = `Max ${minutes(metrics.ground_wait_max_seconds)} · score ${num(metrics.ground_wait_score, 1)} / 100`;
    $('landing').textContent = minutes(landing.landing_wait_mean_seconds);
    const landingOutcomes = landing.landing_wait_by_outcome || {};
    $('landing-note').textContent = `${landingOutcomes.landed?.count || 0} touched down · ${landingOutcomes.pending?.count || 0} pending · ${landingOutcomes.failed?.count || 0} failed/diverting`;
    $('emergency').textContent = minutes(metrics.emergency_wait_mean_seconds);
    const outcomes = metrics.emergency_wait_by_outcome || {};
    $('emergency-note').textContent = `${outcomes.resolved?.count || 0} resolved · ${outcomes.pending?.count || 0} pending · ${outcomes.failed?.count || 0} failed`;
  }

  function renderFeedback() {
    const latest = state.replay?.at(-1);
    $('feedback').replaceChildren();
    if (!latest) {
      $('feedback').append(node('p', 'Your commands and the next two minutes of events will appear here.', 'muted'));
    } else {
      $('feedback').append(node('h3', `DECISION AT T+ ${clock(latest.time_s)}`));
      if (latest.summary) $('feedback').append(node('p', latest.summary));
      const results = node('ul');
      for (const result of latest.command_results || []) {
        const li = node('li');
        li.append(node('code', `${result.accepted ? '✓' : '×'} ${result.command || ''}`, result.accepted ? 'good' : 'bad'));
        if (result.message || result.error) li.append(node('small', result.message || result.error));
        results.append(li);
      }
      if (!(latest.command_results || []).length) results.append(node('li', 'No new commands. Existing clearances continued.', 'muted'));
      $('feedback').append(results, node('h3', 'EVENTS DURING THIS WINDOW'));
      const events = node('ul');
      for (const event of latest.events || []) {
        const li = node('li');
        li.append(node('strong', `T+ ${clock(event.time_s)} · ${(event.type || event.kind || 'event').replaceAll('_', ' ')}`));
        li.append(node('small', event.message || event.description || json(event)));
        events.append(li);
      }
      if (!(latest.events || []).length) events.append(node('li', 'No new events in this control window.', 'muted'));
      $('feedback').append(events);
    }
    $('conversation').replaceChildren();
    for (const message of state.conversation || []) {
      const turn = node('div', undefined, 'conversation-turn');
      turn.append(node('strong', message.role === 'assistant' ? 'CONTROLLER DECISION' : message.role.toUpperCase()), node('pre', typeof message.content === 'string' ? message.content : json(message.content)));
      $('conversation').append(turn);
    }
    if (!(state.conversation || []).length) $('conversation').append(node('p', 'No previous decisions yet.', 'muted'));
  }

  function render(next, resetDraft = false) {
    state = next;
    saveId(state.id);
    $('welcome').hidden = true;
    $('episode').hidden = false;
    $('start').textContent = 'Start new episode ↻';
    $('scenario').value = state.observation.scenario;
    $('seed').value = state.observation.seed;
    $('episode-label').textContent = `${(state.observation.scenario || '').replaceAll('_', ' ').toUpperCase()} · SEED ${state.observation.seed}`;
    $('clock').textContent = `T+ ${clock(state.observation.time_s)} / 30:00`;
    $('turn').textContent = `${state.decision_count || 0} / 15 decisions used`;
    $('progress').value = state.observation.time_s;
    $('system-prompt').textContent = state.system_prompt;
    $('finished').hidden = !state.done;
    $('finished-summary').textContent = `Score ${num(state.observation.metrics?.score, 1)} after ${num(state.decision_count)} decisions. Your run includes every command, plan and observed outcome.`;
    if (resetDraft) {
      $('plan').value = state.latest_plan || 'Prioritize safe separation and urgent arrivals, then reduce waiting. Reassess after each control window.';
      $('commands').value = '';
      $('summary').value = '';
    }
    renderObservation(state.model_observation);
    renderMetrics();
    renderFeedback();
    renderBuilder();
    counts();
    setBusy(busy);
  }

  function commandLines() {
    return $('commands').value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  }

  function aircraftRows() {
    return (state?.model_observation.aircraft || []).map(values => Object.fromEntries(state.model_observation.aircraft_columns.map((key, index) => [key, values[index]])));
  }

  function options(select, values, preferred) {
    select.replaceChildren();
    for (const [value, label] of values) {
      const option = node('option', label);
      option.value = String(value);
      select.append(option);
    }
    if (values.some(([value]) => String(value) === String(preferred))) select.value = String(preferred);
  }

  function renderBuilder() {
    const chosen = $('command-flight').value;
    options($('command-flight'), aircraftRows().map(aircraft => [aircraft.callsign, `${aircraft.callsign} · ${aircraft.type} · ${aircraft.status.replaceAll('_', ' ')}`]), chosen);
    renderCommandValues();
  }

  function renderCommandValues() {
    if (!state) return;
    const action = $('command-action').value;
    const model = state.model_observation;
    const aircraft = aircraftRows().find(item => item.callsign === $('command-flight').value);
    const spec = model.aircraft_types?.[aircraft?.type] || {};
    let choices = [], label = '', preferred;
    if (['APPROACH', 'TAKEOFF'].includes(action)) {
      label = 'Runway';
      choices = (model.airport.runways || []).filter(runway => action === 'APPROACH' ? runway.arrival : runway.departure).map(runway => [runway.id, `${runway.id} · ${runway.length_m} m${model.runway_state?.[runway.id]?.closed ? ' · CLOSED' : ''}`]);
      preferred = `${model.weather?.active_direction || '25'}C`;
    } else if (action === 'HEADING') {
      label = 'Heading'; choices = Array.from({length: 12}, (_, index) => [index * 30, `${index * 30}°`]);
      preferred = 240;
    } else if (action === 'ALTITUDE') {
      label = 'Altitude above airport'; choices = Array.from({length: 18}, (_, index) => [(index + 1) * 1000, `${num((index + 1) * 1000)} ft`]);
      preferred = 5000;
    } else if (action === 'SPEED') {
      label = 'Airspeed';
      const minimum = Number(spec.min_speed_kt || 140), maximum = Number(spec.max_speed_kt || 300);
      choices = [...new Set([minimum, ...Array.from({length: 35}, (_, index) => index * 10).filter(value => value >= minimum && value <= maximum), maximum])].sort((a, b) => a - b).map(value => [value, `${value} kt`]);
      preferred = 220;
    } else if (action === 'DIRECT') {
      label = 'Navigation fix'; choices = Object.keys(model.airport.fixes || {}).map(fix => [fix, fix]);
    }
    $('command-value-field').hidden = choices.length === 0;
    $('command-value-label').textContent = label;
    options($('command-value'), choices, preferred);
  }

  function renderQueue() {
    $('command-queue').replaceChildren();
    const commands = commandLines();
    commands.forEach((command, index) => {
      const chip = node('div', undefined, 'command-chip');
      const remove = node('button', 'Remove');
      remove.type = 'button';
      remove.setAttribute('aria-label', `Remove ${command}`);
      remove.disabled = busy || !state || state.done;
      remove.addEventListener('click', () => {
        const remaining = commandLines();
        remaining.splice(index, 1);
        $('commands').value = remaining.join('\n');
        counts();
      });
      chip.append(node('code', command), remove);
      $('command-queue').append(chip);
    });
    if (!commands.length) $('command-queue').append(node('p', 'No new commands queued. You can advance to let existing clearances continue.', 'field-note'));
  }

  function counts() {
    $('plan-count').textContent = `${$('plan').value.length.toLocaleString()} / 1,200`;
    $('summary-count').textContent = `${$('summary').value.length} / 240`;
    const commands = commandLines();
    $('commands-count').textContent = `${commands.length} / 32`;
    $('commands').setCustomValidity(commands.length > 32 ? 'Use at most 32 commands per decision.' : commands.some(command => command.length > 80) ? 'Each command must be at most 80 characters.' : new Set(commands).size !== commands.length ? 'Remove duplicate commands.' : '');
    $('plan').setCustomValidity($('plan').value.trim() ? '' : 'Write an operational plan before advancing.');
    renderQueue();
  }

  for (const id of ['plan', 'commands', 'summary']) $(id).addEventListener('input', counts);
  for (const id of ['command-flight', 'command-action']) $(id).addEventListener('change', renderCommandValues);
  $('add-command').addEventListener('click', () => {
    if (busy || !state || state.done || !$('command-flight').value) return;
    const command = `${$('command-action').value} ${$('command-flight').value}${$('command-value-field').hidden ? '' : ` ${$('command-value').value}`}`;
    const commands = commandLines();
    if (commands.length >= 32) return notice('Queue at most 32 commands before advancing.');
    if (commands.includes(command)) return notice('That exact command is already queued.');
    $('commands').value = [...commands, command].join('\n');
    notice('');
    counts();
  });

  $('setup').addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    const seed = Number($('seed').value);
    if (!Number.isSafeInteger(seed)) return notice('Use a whole number for the random seed.');
    notice(''); setBusy(true);
    try {
      render(await request('/api/human/start', {scenario: $('scenario').value, seed}), true);
    } catch (error) { notice(error.message); }
    finally { setBusy(false); }
  });

  $('decision').addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || !state || state.done) return;
    counts();
    if (!$('decision').reportValidity()) return;
    notice(''); setBusy(true);
    const previousTime = state.observation.time_s;
    try {
      render(await request('/api/human/step', {
        id: state.id, expected_time_s: previousTime, commands: commandLines(),
        plan: $('plan').value.trim(), summary: $('summary').value.trim()
      }), true);
    } catch (error) {
      let message = error.message;
      // A lost response must not lead to applying the same decision twice.
      try {
        const recovered = await request(`/api/human/state?id=${encodeURIComponent(state.id)}`);
        const advanced = recovered.observation.time_s !== previousTime;
        render(recovered, advanced);
        if (advanced) message += ' The session was refreshed; your decision had already advanced the simulation.';
      } catch { message += ' Refresh the page to recover your session before trying again.'; }
      notice(message);
    } finally { setBusy(false); }
  });

  $('copy-observation').addEventListener('click', async () => {
    if (!state) return;
    try { await navigator.clipboard.writeText(json(state.model_observation)); notice('Copied the exact model observation.'); }
    catch { notice('Clipboard access is unavailable. Select and copy the JSON below.'); }
  });

  $('download').addEventListener('click', () => {
    if (!state) return;
    const blob = new Blob([json({...state, controller: 'human'})], {type: 'application/json'});
    const url = URL.createObjectURL(blob);
    const link = node('a');
    link.href = url;
    link.download = `vector-human-${state.observation.scenario}-${state.observation.seed}.json`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });

  async function restore() {
    const id = savedId();
    if (!id) return setBusy(false);
    setBusy(true);
    try { render(await request(`/api/human/state?id=${encodeURIComponent(id)}`), true); }
    catch (error) {
      if (error.status === 404 || error.status === 410) saveId(null);
      notice(`Previous session could not be restored. ${error.message}`);
    } finally { setBusy(false); }
  }
  restore();
})();
