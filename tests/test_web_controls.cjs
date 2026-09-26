/* Run with: node --test tests/test_web_controls.cjs
 * Exercise the real browser control loop with deferred HTTP and deterministic timers.
 * Canvas/layout are intentionally stubbed; this does not replace browser verification.
 */
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const {test} = require('node:test');
const vm = require('node:vm');

const source = readFileSync(join(__dirname, '..', 'atc_bench', 'web', 'app.js'), 'utf8');
const state = (controller = {}, time = 0) => ({
  time_s: time, duration_s: 1800, seed: 7, scenario: 'mixed', aircraft: [],
  airport: {runways: []}, weather: {active_direction: '25', wind_from_deg: 250},
  metrics: {}, events: [], controller: {
    kind: 'lmstudio', model: 'mock-model', status: 'idle', decision_count: 0,
    last_decision: null, error: null, ...controller,
  },
});
const ready = () => state({status: 'ready', decision_count: 1,
  last_decision: {commands: [], summary: '', latency_s: 12, plan: 'Reserve 25R for the emergency; release departures from 18.', memory_turns: 1}}, 30);
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

function harness(initial = state()) {
  const elements = new Map(), timers = new Map(), intervals = new Map();
  const requests = [], pendingRequests = [];
  let nextId = 0, now = 0;
  function element() {
    const node = {
      value: '', textContent: '', hidden: false, disabled: false, checked: false,
      children: [], options: [{value: 'mixed'}], style: {}, events: {},
      classList: {toggle() {}}, parentElement: {classList: {toggle() {}}},
      getContext() { return {}; }, addEventListener(name, fn) { this.events[name] = fn; },
      setAttribute() {}, querySelector() { return {style: {}}; }, focus() {}, add() {},
      append(row) { this.children.push(row); row.remove = () => this.children.splice(this.children.indexOf(row), 1); },
    };
    Object.defineProperty(node, 'firstElementChild', {get: () => node.children[0]});
    Object.defineProperty(node, 'innerHTML', {
      get: () => node.html || '', set: (value) => { node.html = value; node.children = []; },
    });
    return node;
  }
  function get(id) {
    if (!elements.has(id)) {
      const node = element();
      if (id === 'autopilot') node.checked = true;
      if (id === 'speed') node.value = '10';
      if (id === 'decision-interval') node.value = '120';
      if (id === 'controller-kind') node.value = 'lmstudio';
      elements.set(id, node);
    }
    return elements.get(id);
  }
  const response = (body, status = 200) => ({ok: status === 200, status, json: async () => structuredClone(body)});
  vm.runInNewContext(source, {
    document: {getElementById: get, querySelectorAll: () => [], createElement: element},
    window: {addEventListener() {}}, ResizeObserver: class { observe() {} }, Option: class {},
    performance: {now: () => now},
    setTimeout(fn, delay) { const id = ++nextId; timers.set(id, {fn, delay}); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval(fn, delay) { const id = ++nextId; intervals.set(id, {fn, delay}); return id; },
    clearInterval(id) { intervals.delete(id); },
    fetch(path, options) {
      requests.push({path, body: options?.body ? JSON.parse(options.body) : null});
      if (requests.length === 1) return Promise.resolve(response(initial));
      return new Promise((resolve, reject) => pendingRequests.push({resolve, reject}));
    },
  });
  return {
    get, timers, intervals, requests,
    click(id) { assert.equal(get(id).disabled, false, `${id} must be enabled`); return get(id).events.click(); },
    tick() { const [id, timer] = timers.entries().next().value; timers.delete(id); now += timer.delay; return timer.fn(); },
    resolve(body, status) { pendingRequests.shift().resolve(response(body, status)); },
    reject(message) { pendingRequests.shift().reject(new Error(message)); },
  };
}

test('Run begins immediately; Pause during inference permits only the current decision', async () => {
  const ui = harness(); await flush();
  assert.equal(ui.get('notice').textContent, '');
  ui.get('speed').value = '1';
  ui.click('play');
  assert.equal([...ui.timers.values()][0].delay, 0);
  const request = ui.tick(); await flush();
  assert.deepEqual(ui.requests[1].body, {seconds: 120, commands: [], autopilot: true});
  ui.click('play');
  assert.equal(ui.get('radar-state').textContent, 'PAUSING');
  ui.resolve(ready()); await request;
  assert.equal(ui.timers.size, 0);
  assert.equal(ui.intervals.size, 0);
  assert.equal(ui.requests.length, 2);
  assert.match(ui.get('controller-summary').textContent, /No commands/);
  assert.equal(ui.get('controller-timing').textContent, '1 decisions · last call 12.0s · 1 turns in memory');
  assert.match(ui.get('controller-plan').textContent, /Reserve 25R/);
  assert.match(ui.get('decision-window-note').textContent, /120 simulated seconds/);
});

test('LLM decisions continue immediately after accelerated steps and failures stop the loop', async () => {
  const ui = harness(); await flush();
  ui.get('speed').value = '1'; ui.click('play');
  const first = ui.tick(); await flush(); ui.resolve(ready()); await first;
  assert.equal([...ui.timers.values()][0].delay, 0);
  const second = ui.tick(); await flush();
  assert.equal(ui.requests[2].body.seconds, 120);
  ui.resolve({error: 'Model offline', controller: state({status: 'error', error: 'Model offline'}).controller}, 502);
  await second;
  assert.equal(ui.timers.size, 0);
  assert.equal(ui.get('controller-status').textContent, 'ERROR');
  assert.equal(ui.get('notice').textContent, 'Model offline');
  assert.equal(ui.get('sim-clock').textContent, 'T+ 00:00:30');
});

test('reload during external inference polls to completion and serializes controls', async () => {
  const ui = harness(state({status: 'thinking'})); await flush();
  for (const id of ['play', 'step', 'reset', 'send-command', 'apply-controller']) assert.equal(ui.get(id).disabled, true, id);
  const first = ui.tick(); await flush();
  assert.equal(ui.requests[1].path, '/api/state');
  ui.resolve(state({status: 'thinking'})); await first;
  assert.equal(ui.timers.size, 1);
  assert.match(ui.get('controller-timing').textContent, /Observing for 1.0s/);
  const second = ui.tick(); await flush(); ui.resolve(ready()); await second;
  assert.equal(ui.timers.size, 0);
  assert.equal(ui.get('controller-status').textContent, 'READY');
  assert.equal(ui.get('play').disabled, false);
  assert.equal(ui.get('sim-clock').textContent, 'T+ 00:00:30');
  assert.equal(ui.requests.filter((r) => r.body !== null).length, 0);
});

test('a concurrent-request conflict observes the existing decision and displays its failure', async () => {
  const ui = harness(); await flush(); ui.click('play');
  const request = ui.tick(); await flush();
  ui.resolve({error: 'A decision is in progress', controller: state({status: 'thinking'}).controller}, 409);
  await request;
  assert.equal(ui.get('play').disabled, true);
  const poll = ui.tick(); await flush();
  ui.resolve(state({status: 'error', error: 'Model timed out'})); await poll;
  assert.equal(ui.timers.size, 0);
  assert.equal(ui.get('controller-status').textContent, 'ERROR');
  assert.equal(ui.get('notice').textContent, 'Model timed out');
  assert.equal(ui.get('play').disabled, false);
  assert.equal(ui.requests.filter((r) => r.path === '/api/step').length, 1);
});

test('a failed status poll keeps mutations disabled and retries until state is readable', async () => {
  const ui = harness(state({status: 'thinking'})); await flush();
  const failed = ui.tick(); await flush(); ui.reject('Connection interrupted'); await failed;
  assert.equal(ui.get('notice').textContent, 'Connection interrupted');
  assert.equal(ui.get('reset').disabled, true);
  assert.equal(ui.timers.size, 1);
  const retried = ui.tick(); await flush(); ui.resolve(ready()); await retried;
  assert.equal(ui.get('notice').textContent, '');
  assert.equal(ui.get('reset').disabled, false);
  assert.equal(ui.timers.size, 0);
});

test('OpenRouter runs as an accelerated LLM and displays spend without exposing credentials', async () => {
  const hosted = state({kind: 'openrouter', model: 'z-ai/glm-5.3-flash',
    budget: {limit_usd: 10, spent_usd: .02, reserved_usd: .01, remaining_usd: 9.97}});
  const ui = harness(hosted); await flush();
  assert.equal(ui.get('controller-label').textContent, 'Hosted OpenRouter controller');
  assert.equal(ui.get('controller-model').disabled, false);
  assert.equal(ui.get('speed').disabled, true);
  assert.match(ui.get('controller-cost').textContent, /Spent \$0.0200 \/ \$10.00/);
  assert.match(ui.get('controller-cost').textContent, /\$0.0100 reserved/);
  ui.click('play'); const request = ui.tick(); await flush();
  assert.equal(ui.get('controller-status').textContent, 'THINKING');
  assert.equal(ui.requests[1].body.seconds, 120);
  assert.equal(ui.get('sim-clock').textContent, 'T+ 00:00:00');
  ui.click('play');
  ui.resolve({...hosted, time_s: 120, controller: {...hosted.controller, status: 'ready', decision_count: 1,
    last_decision: {commands: [], plan: 'Protect emergency runway', memory_turns: 1, cost_usd: .000123}}});
  await request;
  assert.equal(ui.timers.size, 0);
  assert.equal(ui.get('sim-clock').textContent, 'T+ 00:02:00');
  assert.match(ui.get('controller-cost').textContent, /last call \$0.000123/);
  assert.equal(ui.get('controller-plan').textContent, 'Protect emergency runway');
});

test('switching to OpenRouter chooses its default model and sends no local base URL', async () => {
  const ui = harness(); await flush();
  ui.get('controller-kind').value = 'openrouter';
  ui.get('controller-kind').events.change();
  assert.equal(ui.get('controller-model').value, 'z-ai/glm-5.3-flash');
  const connected = ui.click('apply-controller'); await flush();
  assert.deepEqual(ui.requests[1], {path: '/api/controller', body: {kind: 'openrouter', model: 'z-ai/glm-5.3-flash'}});
  ui.resolve(state({kind: 'openrouter', model: 'z-ai/glm-5.3-flash'})); await connected;
  assert.equal(ui.get('controller-kind').value, 'openrouter');
  ui.get('controller-kind').value = 'lmstudio';
  ui.get('controller-kind').events.change();
  assert.equal(ui.get('controller-model').value, 'mock-model');
  const local = ui.click('apply-controller'); await flush();
  assert.equal(ui.requests[2].body.base_url, 'http://127.0.0.1:1234');
  ui.resolve(state()); await local;
  assert.equal(ui.get('controller-cost').hidden, true);
});

test('unavailable hosted budget is visible instead of showing stale remaining credit', async () => {
  const ui = harness(state({kind: 'openrouter', model: 'z-ai/glm-5.3-flash',
    budget_error: 'OpenRouter budget information is unavailable.',
    last_decision: {commands: [], budget: {spent_usd: 0, remaining_usd: 10, limit_usd: 10}}}));
  await flush();
  assert.equal(ui.get('controller-cost').hidden, false);
  assert.match(ui.get('controller-cost').textContent, /unavailable/);
  assert.doesNotMatch(ui.get('controller-cost').textContent, /available of|\$10/);
});

test('a blocked hosted budget does not advertise remaining credit as available', async () => {
  const ui = harness(state({kind: 'openrouter', model: 'z-ai/glm-5.3-flash', budget_blocked: true,
    budget: {spent_usd: 2, remaining_usd: 8, limit_usd: 10}}));
  await flush();
  assert.match(ui.get('controller-cost').textContent, /blocks further model requests/);
  assert.doesNotMatch(ui.get('controller-cost').textContent, /available/);
});
