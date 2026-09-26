(() => {
  'use strict';
  const number = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  const groundStates = new Set(['ground', 'landing_roll', 'taxi_in', 'takeoff_roll']);
  const finished = new Set(['landed', 'departed', 'diverted', 'crashed']);
  const ranges = [5, 10, 20, 30, 40, 50, 60];

  // The same radar visual language as app.js, fed only the model's current input.
  class HumanRadar {
    constructor(canvas, {onSelect} = {}) {
      this.canvas = canvas;
      this.context = canvas.getContext('2d');
      this.onSelect = onSelect;
      this.range = 40;
      this.width = 0;
      this.height = 0;
      this.selected = null;
      this.observation = null;
      this.sessionId = null;
      this.trails = new Map();
      this.hitTargets = [];
      this.resizeObserver = new ResizeObserver(() => this.resize());
      this.resizeObserver.observe(canvas);
      canvas.addEventListener('click', event => {
        const bounds = canvas.getBoundingClientRect();
        const x = event.clientX - bounds.left, y = event.clientY - bounds.top;
        const target = [...this.hitTargets].reverse().find(item => Math.hypot(item.x - x, item.y - y) < 13 ||
          (x >= item.left && x <= item.right && y >= item.top && y <= item.bottom));
        if (target) { this.select(target.callsign); this.onSelect?.(target.callsign); }
      });
      this.resize();
    }
    update(model, selectedCallsign = null, sessionId = null) {
      if (sessionId !== this.sessionId) { this.trails.clear(); this.sessionId = sessionId; this.range = 40; }
      const aircraft = (model?.aircraft || []).map(row => Object.fromEntries((model.aircraft_columns || []).map((key, index) => [key, row[index]])));
      for (const aircraftItem of aircraft) {
        const history = this.trails.get(aircraftItem.callsign) || [];
        if (history.at(-1)?.[2] !== model.time_s) history.push([number(aircraftItem.x_nm), number(aircraftItem.y_nm), model.time_s]);
        this.trails.set(aircraftItem.callsign, history.slice(-35));
      }
      this.observation = model ? {...model, aircraft} : null;
      this.selected = selectedCallsign;
      this.draw();
    }
    select(callsign) { this.selected = callsign; this.draw(); }
    zoom(delta) {
      this.range = ranges[Math.max(0, Math.min(ranges.length - 1, ranges.indexOf(this.range) + delta))];
      this.draw();
      return this.range;
    }
    reset() { this.range = 40; this.draw(); return this.range; }
    resize() {
      const bounds = this.canvas.getBoundingClientRect();
      this.width = bounds.width; this.height = bounds.height;
      const ratio = window.devicePixelRatio || 1;
      this.canvas.width = Math.round(this.width * ratio);
      this.canvas.height = Math.round(this.height * ratio);
      this.context.setTransform(ratio, 0, 0, ratio, 0, 0);
      this.draw();
    }
    project(x, y) {
      const scale = (Math.min(this.width, this.height) - 44) / (2 * this.range);
      return [this.width / 2 + number(x) * scale, this.height / 2 - number(y) * scale];
    }
    radius(nm) { return number(nm) * (Math.min(this.width, this.height) - 44) / (2 * this.range); }
    line(points, color, width = 1, dash = []) {
      if (!points.length) return;
      const context = this.context;
      context.beginPath(); context.setLineDash(dash); context.strokeStyle = color; context.lineWidth = width;
      points.forEach((point, index) => index ? context.lineTo(...point) : context.moveTo(...point));
      context.stroke(); context.setLineDash([]);
    }
    label(text, x, y, color = '#6d8c9c', font = '9px "IBM Plex Mono", monospace') {
      this.context.font = font; this.context.fillStyle = color; this.context.fillText(String(text), x, y);
    }
    circle(x, y, radius, color, dash = []) {
      const context = this.context;
      context.beginPath(); context.arc(x, y, Math.max(0, radius), 0, Math.PI * 2);
      context.strokeStyle = color; context.lineWidth = 1; context.setLineDash(dash); context.stroke(); context.setLineDash([]);
    }
    drawBackground() {
      const context = this.context, range = this.range;
      context.fillStyle = '#101920'; context.fillRect(0, 0, this.width, this.height);
      const [cx, cy] = this.project(0, 0);
      const grid = range <= 10 ? 2 : 10;
      const extent = range * Math.max(1, this.width / Math.max(1, this.height)) + grid;
      for (let x = -Math.ceil(extent / grid) * grid; x <= extent; x += grid) this.line([this.project(x, -extent), this.project(x, extent)], '#1b2a34', .65);
      for (let y = -Math.ceil(extent / grid) * grid; y <= extent; y += grid) this.line([this.project(-extent, y), this.project(extent, y)], '#1b2a34', .65);
      for (let radius = grid; radius <= range; radius += grid) {
        this.circle(cx, cy, this.radius(radius), radius === range ? '#304753' : '#263b48', radius === range ? [3, 5] : []);
        this.label(`${radius} NM`, cx + this.radius(radius) * .7 + 5, cy - this.radius(radius) * .7 - 4, '#415f71', '8px "IBM Plex Mono", monospace');
      }
      for (let angle = 0; angle < 360; angle += 30) {
        const rad = angle * Math.PI / 180;
        this.line([[cx + this.radius(range - 1) * Math.sin(rad), cy - this.radius(range - 1) * Math.cos(rad)], [cx + this.radius(range) * Math.sin(rad), cy - this.radius(range) * Math.cos(rad)]], '#506673', .8);
      }
      this.line([[cx - 7, cy], [cx + 7, cy]], '#55717e', .8);
      this.line([[cx, cy - 7], [cx, cy + 7]], '#55717e', .8);
      this.label('N', cx - 3, 18, '#9db4c0');
      this.label(`${range} NM RANGE`, 14, this.height - 34, '#9db4c0');
    }
    drawWeather() {
      const context = this.context;
      for (const cell of this.observation.weather?.cells || []) {
        const [x, y] = this.project(cell.x_nm, cell.y_nm), radius = this.radius(cell.radius_nm);
        context.save(); context.beginPath(); context.arc(x, y, Math.max(0, radius), 0, Math.PI * 2); context.fillStyle = '#80613020'; context.fill();
        context.strokeStyle = '#a7834e75'; context.lineWidth = 1; context.setLineDash([4, 4]); context.stroke(); context.setLineDash([]); context.clip();
        for (let offset = -radius * 2; offset < radius * 2; offset += 10) this.line([[x - radius + offset, y - radius], [x + radius + offset, y + radius]], '#a7834e22');
        context.restore();
        this.label(`WX ${cell.id}`, x - 18, y - 3, '#bb9967', '8px "IBM Plex Mono", monospace');
        this.label(String(cell.severity).toUpperCase(), x - 18, y + 10, '#8f7956', '7px "IBM Plex Mono", monospace');
      }
    }
    drawAirport() {
      const context = this.context, airport = this.observation.airport || {};
      for (const [name, coordinates] of Object.entries(airport.fixes || {})) {
        const [x, y] = this.project(...coordinates);
        context.beginPath(); context.moveTo(x, y - 4); context.lineTo(x + 3.5, y + 3); context.lineTo(x - 3.5, y + 3); context.closePath(); context.strokeStyle = '#4b6776'; context.lineWidth = .8; context.stroke();
        this.label(name, x + 7, y + 3, '#526e7e', '8px "IBM Plex Mono", monospace');
      }
      const direction = this.observation.weather?.active_direction;
      const runways = (airport.runways || []).map(runway => ({...runway, ...this.observation.runway_state?.[runway.id]}))
        .filter(runway => !direction || String(runway.id).startsWith(direction) || runway.id === '18');
      for (const runway of runways) {
        const start = this.project(...runway.threshold), end = this.project(...runway.end);
        const color = runway.closed ? '#f28086' : runway.occupied_by ? '#d7a35f' : '#c1d0d5';
        if (runway.arrival && runway.approach_fix) this.line([start, this.project(...runway.approach_fix)], '#8eb2bb38', .8, [3, 4]);
        this.line([start, end], '#0b1216', Math.max(4, this.radius(.18)));
        this.line([start, end], color, Math.max(1.5, this.radius(.10)), runway.closed ? [3, 3] : []);
        if (this.range <= 20) this.label(`${runway.id}${runway.closed ? ' CLOSED' : ''}`, start[0] + 5, start[1] - 6, color, '8px "IBM Plex Mono", monospace');
      }
      const [cx, cy] = this.project(0, 0);
      if (this.range > 20) this.label('EDDF', cx - 10, cy + 28, '#a4bdc7');
      const closed = runways.filter(runway => runway.closed).map(runway => runway.id);
      if (closed.length) this.label(`CLOSED: ${closed.join(' / ')}`, 14, this.height - 16, '#f28086');
    }
    drawConflicts() {
      for (const conflict of this.observation.conflicts || []) {
        const planes = (conflict.callsigns || []).map(callsign => this.observation.aircraft.find(item => item.callsign === callsign)).filter(Boolean);
        if (planes.length !== 2) continue;
        const points = planes.map(item => this.project(item.x_nm, item.y_nm));
        this.line(points, '#ff76788c', 1, [4, 3]);
        for (const point of points) this.circle(...point, 17, '#ff767899');
        this.label(`${number(conflict.distance_nm).toFixed(1)} NM`, (points[0][0] + points[1][0]) / 2 + 5, (points[0][1] + points[1][1]) / 2 - 5, '#ff9292', '8px "IBM Plex Mono", monospace');
      }
    }
    drawAircraft() {
      const context = this.context;
      const flights = this.observation.aircraft.filter(item => !finished.has(item.status));
      flights.sort((a, b) => Number(a.callsign === this.selected) - Number(b.callsign === this.selected));
      const groundLabels = [];
      for (const aircraft of flights) {
        const [x, y] = this.project(aircraft.x_nm, aircraft.y_nm);
        const grounded = groundStates.has(aircraft.status), selected = aircraft.callsign === this.selected;
        const color = aircraft.emergency ? '#fba65e' : aircraft.kind === 'departure' ? '#84afff' : '#63dfc8';
        const history = this.trails.get(aircraft.callsign) || [];
        if (!grounded && history.length > 1) this.line(history.map(point => this.project(...point)), `${color}35`);
        const radians = number(aircraft.heading_deg) * Math.PI / 180;
        if (!grounded) { const vector = this.radius(number(aircraft.speed_kt) / 60 * 1.5); this.line([[x, y], [x + Math.sin(radians) * vector, y - Math.cos(radians) * vector]], `${color}66`, .8); }
        if (selected) {
          this.circle(x, y, 14, `${color}99`);
          if (!grounded) {
            this.circle(x, y, this.radius(3), `${color}80`, [3, 4]);
            this.label('3 NM', x + this.radius(3) + 4, y - 3, `${color}b3`, '8px "IBM Plex Mono", monospace');
            if (aircraft.target_heading_deg != null) { const target = number(aircraft.target_heading_deg) * Math.PI / 180; this.line([[x, y], [x + Math.sin(target) * 42, y - Math.cos(target) * 42]], `${color}88`, .8, [3, 3]); }
          }
        }
        context.save(); context.translate(x, y); context.rotate(radians); context.fillStyle = color;
        if (grounded) context.fillRect(-2.5, -2.5, 5, 5);
        else { context.beginPath(); context.moveTo(0, -6); context.lineTo(4, 4); context.lineTo(0, 2); context.lineTo(-4, 4); context.closePath(); context.fill(); }
        context.restore();
        const labelX = x + (x > this.width - 118 ? -104 : 14);
        let labelY = y - 11;
        if (grounded) {
          while (groundLabels.some(label => Math.abs(label.x - labelX) < 105 && Math.abs(label.y - labelY) < 29)) labelY += 29;
          groundLabels.push({x: labelX, y: labelY});
          if (labelY !== y - 11) this.line([[x + 4, y + 4], [labelX - 3, labelY + 1]], `${color}35`, .7);
        }
        const textWidth = Math.max(80, String(aircraft.callsign).length * 7 + 5);
        context.fillStyle = selected ? '#182b30ed' : '#101920b8'; context.fillRect(labelX - 3, labelY - 10, textWidth, 27);
        this.label(aircraft.callsign, labelX, labelY, color, `${selected ? '500 ' : ''}10px "IBM Plex Mono", monospace`);
        const tag = grounded ? String(aircraft.status).replaceAll('_', ' ').toUpperCase() : `${String(Math.round(number(aircraft.altitude_ft) / 100)).padStart(3, '0')}  ${Math.round(number(aircraft.speed_kt))}KT`;
        this.label(tag, labelX, labelY + 12, `${color}a6`, '8px "IBM Plex Mono", monospace');
        if (aircraft.emergency) { context.fillStyle = color; context.fillRect(labelX - 3, labelY - 19, 34, 5); }
        this.hitTargets.push({callsign: aircraft.callsign, x, y, left: labelX - 4, top: labelY - 12, right: labelX + textWidth, bottom: labelY + 17});
      }
    }
    draw() {
      if (this.width <= 44 || this.height <= 44) return;
      this.hitTargets = [];
      this.drawBackground();
      if (this.observation) { this.drawWeather(); this.drawAirport(); this.drawConflicts(); this.drawAircraft(); }
      else this.label('Start an episode to view current traffic', Math.max(14, this.width / 2 - 145), this.height / 2 - 12, '#9db4c0');
    }
  }
  window.HumanRadar = HumanRadar;
})();
