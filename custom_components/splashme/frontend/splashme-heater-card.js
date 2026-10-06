/**
 * SplashMe Heater Card — target temperature dial plus the heating sources
 * the firmware may use, one tab per body of water (Pool / Spa), mirroring
 * the app's temperature pages.
 *
 * Bundled with the splashme integration; registered automatically.
 *
 * Example:
 *   type: custom:splashme-heater-card
 *   water_entity: sensor.water_temperature          # optional, shared
 *   ambient_entity: sensor.ambient_temperature      # optional, shared
 *   tabs:
 *     - title: Pool
 *       target_entity: number.pool_target_temperature
 *       heaters:
 *         - name: Pool Heater
 *           enable_entity: switch.pool_heater
 *           state_entity: binary_sensor.heater      # optional, running dot
 *     - title: Spa
 *       target_entity: number.spa_target_temperature
 *       heaters: [...]
 */

const ARC_START = 225; // degrees clockwise from 12 o'clock: 7:30 round the top to 4:30
const ARC_SWEEP = 270;
const ARC_R = 78;
const WRITE_DELAY_MS = 800; // coalesce +/- taps into one device write
const PENDING_MS = 15000; // show the requested value until the device confirms

class SplashmeHeaterCard extends HTMLElement {
  setConfig(config) {
    const tabs = config.tabs || (config.target_entity ? [config] : []);
    if (!tabs.length || tabs.some((t) => !t.target_entity)) {
      throw new Error("splashme-heater-card: every tab needs a 'target_entity'");
    }
    this._config = { ...config, tabs: tabs.map((t) => ({ heaters: [], ...t })) };
    this._tab = 0;
    this._built = false;
    this._pending = null; // {entity, value, until}
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._built) this._build();
    this._update();
  }

  get _current() {
    return this._config.tabs[this._tab];
  }

  _build() {
    this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card { padding: 12px 16px 14px; }
        .tabs { display: flex; justify-content: center; gap: 6px; margin-bottom: 8px; }
        .tabs button { border: none; background: none; font: inherit; font-size: 16px; font-weight: 500; padding: 6px 14px; border-radius: 16px; color: var(--secondary-text-color); cursor: pointer; }
        .tabs button.active { background: rgba(77, 208, 196, 0.18); color: var(--primary-text-color); }
        .tabs.single button { cursor: default; }
        .temps { display: flex; justify-content: center; gap: 32px; margin-bottom: 6px; font-size: 19px; font-weight: 600; }
        .temps span { display: inline-flex; align-items: center; gap: 6px; }
        .temps ha-icon { --mdc-icon-size: 22px; }
        .ambient ha-icon { color: #f5b400; }
        .water ha-icon { color: #4dd0c4; }
        .heaters { margin: 0 6px; }
        .heater { display: flex; align-items: center; padding: 5px 0; font-size: 16px; cursor: pointer; }
        .heater.unavailable { opacity: 0.4; cursor: default; }
        .dot { width: 12px; height: 12px; border-radius: 50%; background: var(--disabled-color, #9e9e9e); margin-right: 12px; }
        .dot.on { background: #4caf50; box-shadow: 0 0 6px #4caf50; }
        .heater .name { flex: 1; }
        .check { width: 24px; height: 24px; border-radius: 50%; border: 2px solid var(--secondary-text-color, #888); display: grid; place-items: center; }
        .check ha-icon { --mdc-icon-size: 16px; display: none; color: #4caf50; }
        .check.on { border-color: #4caf50; }
        .check.on ha-icon { display: block; }
        .dial { display: grid; grid-template-columns: 44px 1fr 44px; align-items: center; }
        svg { width: 100%; max-width: 190px; margin: 0 auto; display: block; touch-action: none; }
        .track { fill: none; stroke: var(--divider-color, #9e9e9e); stroke-width: 12; stroke-linecap: round; }
        .fill { fill: none; stroke: url(#splashme-heat); stroke-width: 12; stroke-linecap: round; }
        .knob { fill: #4dd0c4; stroke: var(--card-background-color, #fff); stroke-width: 3; cursor: grab; }
        .value { font-size: 28px; font-weight: 600; fill: #4dd0c4; text-anchor: middle; }
        button.step { width: 40px; height: 40px; border-radius: 50%; border: 2px solid #4dd0c4; background: none; color: #4dd0c4; font-size: 24px; line-height: 1; cursor: pointer; }
        button.step:disabled, .dial.unavailable .knob { opacity: 0.4; cursor: default; }
        .label { text-align: center; font-size: 16px; margin-top: 2px; }
        .note { text-align: center; font-size: 12px; color: var(--secondary-text-color); margin-top: 4px; }
      </style>
      <ha-card>
        <div class="tabs"></div>
        <div class="temps">
          <span class="ambient"><ha-icon icon="mdi:white-balance-sunny"></ha-icon><b>--°</b></span>
          <span class="water"><ha-icon icon="mdi:waves"></ha-icon><b>--°</b></span>
        </div>
        <div class="heaters"></div>
        <div class="dial">
          <button class="step minus" aria-label="Lower">−</button>
          <svg viewBox="0 0 200 200">
            <defs>
              <linearGradient id="splashme-heat" x1="0" y1="1" x2="1" y2="0">
                <stop offset="0" stop-color="#4dd0c4"></stop>
                <stop offset="0.55" stop-color="#8bc34a"></stop>
                <stop offset="1" stop-color="#ffb300"></stop>
              </linearGradient>
            </defs>
            <path class="track"></path>
            <path class="fill"></path>
            <circle class="knob" r="13"></circle>
            <text class="value" x="100" y="186">--</text>
          </svg>
          <button class="step plus" aria-label="Raise">+</button>
        </div>
        <div class="label"></div>
        <div class="note" hidden></div>
      </ha-card>
    `;
    const root = this.shadowRoot;
    root.querySelector(".tabs").addEventListener("click", (ev) => {
      const btn = ev.target.closest("button");
      if (btn) {
        this._tab = Number(btn.dataset.index);
        this._update();
      }
    });
    root.querySelector(".minus").addEventListener("click", () => this._step(-1));
    root.querySelector(".plus").addEventListener("click", () => this._step(1));
    root.querySelector(".heaters").addEventListener("click", (ev) => {
      const row = ev.target.closest(".heater");
      if (row && !row.classList.contains("unavailable")) this._toggleHeater(row.dataset.entity);
    });
    const svg = root.querySelector("svg");
    svg.addEventListener("pointerdown", (ev) => this._dragStart(ev));
    svg.addEventListener("pointermove", (ev) => this._dragMove(ev));
    svg.addEventListener("pointerup", (ev) => this._dragEnd(ev));
    svg.addEventListener("pointercancel", (ev) => this._dragEnd(ev));
    root.querySelector(".track").setAttribute("d", this._arcPath(ARC_START, ARC_START + ARC_SWEEP));
    this._built = true;
  }

  // -- state helpers -------------------------------------------------------

  _state(entityId) {
    if (!entityId || !this._hass) return null;
    const st = this._hass.states[entityId];
    return !st || st.state === "unavailable" || st.state === "unknown" ? null : st;
  }

  _range() {
    const st = this._state(this._current.target_entity);
    const attrs = (st && st.attributes) || {};
    return {
      min: Number.isFinite(attrs.min) ? attrs.min : 0,
      max: Number.isFinite(attrs.max) ? attrs.max : 40,
      step: Number.isFinite(attrs.step) && attrs.step > 0 ? attrs.step : 1,
    };
  }

  _target() {
    const entity = this._current.target_entity;
    const st = this._state(entity);
    const p = this._pending;
    if (p && p.entity === entity) {
      if (st && parseFloat(st.state) === p.value) this._pending = null; // write landed
      else if (Date.now() < p.until) return p.value;
      else this._pending = null;
    }
    if (!st) return null;
    const value = parseFloat(st.state);
    return Number.isFinite(value) ? value : null;
  }

  // -- rendering -----------------------------------------------------------

  _polar(deg) {
    const rad = ((deg - 90) * Math.PI) / 180;
    return { x: 100 + ARC_R * Math.cos(rad), y: 100 + ARC_R * Math.sin(rad) };
  }

  _arcPath(from, to) {
    if (to - from < 0.01) return "";
    const a = this._polar(from);
    const b = this._polar(to);
    return `M ${a.x} ${a.y} A ${ARC_R} ${ARC_R} 0 ${to - from > 180 ? 1 : 0} 1 ${b.x} ${b.y}`;
  }

  _update() {
    if (!this._built || !this._hass) return;
    const root = this.shadowRoot;
    const cfg = this._config;
    const tab = this._current;

    const tabs = root.querySelector(".tabs");
    tabs.className = `tabs ${cfg.tabs.length === 1 ? "single" : ""}`;
    tabs.innerHTML = cfg.tabs
      .map((t, i) => `<button data-index="${i}" class="${i === this._tab ? "active" : ""}">${t.title || `Tab ${i + 1}`}</button>`)
      .join("");

    // HA converts temperature entities to the instance's unit system, so show the
    // unit the state actually carries instead of assuming °C.
    const unitOf = (st) => (st && st.attributes && st.attributes.unit_of_measurement) || "°C";
    const fmt = (entityId) => {
      const st = this._state(entityId);
      const v = st ? parseFloat(st.state) : NaN;
      return Number.isFinite(v) ? `${v.toFixed(1)}${unitOf(st)}` : "--°";
    };
    root.querySelector(".ambient b").textContent = fmt(tab.ambient_entity || cfg.ambient_entity);
    root.querySelector(".water b").textContent = fmt(tab.water_entity || cfg.water_entity);

    root.querySelector(".heaters").innerHTML = tab.heaters
      .map((h) => {
        const enable = this._state(h.enable_entity);
        const running = this._state(h.state_entity);
        return `<div class="heater ${enable ? "" : "unavailable"}" data-entity="${h.enable_entity}">
            <span class="dot ${running && running.state === "on" ? "on" : ""}"></span>
            <span class="name">${h.name || h.enable_entity}</span>
            <span class="check ${enable && enable.state === "on" ? "on" : ""}"><ha-icon icon="mdi:check"></ha-icon></span>
          </div>`;
      })
      .join("");

    const target = this._target();
    const { min, max } = this._range();
    const available = target != null;
    root.querySelector(".dial").classList.toggle("unavailable", !available);
    root.querySelectorAll("button.step").forEach((b) => (b.disabled = !available));
    const frac = available ? Math.max(0, Math.min(1, (target - min) / (max - min || 1))) : 0;
    const end = ARC_START + ARC_SWEEP * frac;
    root.querySelector(".fill").setAttribute("d", this._arcPath(ARC_START, end));
    const knob = this._polar(end);
    const knobEl = root.querySelector(".knob");
    knobEl.setAttribute("cx", knob.x);
    knobEl.setAttribute("cy", knob.y);
    root.querySelector(".value").textContent = available ? `${target} ${unitOf(this._state(tab.target_entity))}` : "--";
    root.querySelector(".label").textContent = tab.label || `Set ${tab.title || ""} Temperature`.replace("  ", " ");
    const note = root.querySelector(".note");
    note.hidden = available;
    note.textContent = "Waiting for the device…";
  }

  // -- writes ----------------------------------------------------------------

  _step(direction) {
    const current = this._target();
    if (current == null) return;
    const { min, max, step } = this._range();
    this._setTarget(Math.max(min, Math.min(max, current + direction * step)));
  }

  _setTarget(value) {
    const entity = this._current.target_entity;
    this._pending = { entity, value, until: Date.now() + PENDING_MS };
    this._update();
    clearTimeout(this._writeTimer);
    this._writeTimer = setTimeout(() => {
      this._hass.callService("number", "set_value", { entity_id: entity, value });
    }, WRITE_DELAY_MS);
  }

  _valueAt(ev) {
    const svg = this.shadowRoot.querySelector("svg");
    const box = svg.getBoundingClientRect();
    const x = ((ev.clientX - box.left) / box.width) * 200 - 100;
    const y = ((ev.clientY - box.top) / box.height) * 200 - 100;
    let deg = (Math.atan2(y, x) * 180) / Math.PI + 90; // clockwise from 12 o'clock
    if (deg < 0) deg += 360;
    let along = deg - ARC_START;
    if (along < 0) along += 360;
    if (along > ARC_SWEEP) along = along - ARC_SWEEP < (360 - ARC_SWEEP) / 2 ? ARC_SWEEP : 0;
    const { min, max, step } = this._range();
    const raw = min + (along / ARC_SWEEP) * (max - min);
    return Math.max(min, Math.min(max, Math.round(raw / step) * step));
  }

  _dragStart(ev) {
    if (this._target() == null) return;
    this._dragging = true;
    ev.currentTarget.setPointerCapture(ev.pointerId);
    this._pending = { entity: this._current.target_entity, value: this._valueAt(ev), until: Date.now() + PENDING_MS };
    this._update();
  }

  _dragMove(ev) {
    if (!this._dragging) return;
    this._pending = { entity: this._current.target_entity, value: this._valueAt(ev), until: Date.now() + PENDING_MS };
    this._update();
  }

  _dragEnd(ev) {
    if (!this._dragging) return;
    this._dragging = false;
    ev.currentTarget.releasePointerCapture(ev.pointerId);
    this._setTarget(this._pending.value);
  }

  _toggleHeater(entityId) {
    const st = this._state(entityId);
    if (!st) return;
    this._hass.callService("switch", st.state === "on" ? "turn_off" : "turn_on", { entity_id: entityId });
  }

  getCardSize() {
    return 6;
  }
}

// See splashme-tank-card.js: register on whichever custom element registry
// is live once the Home Assistant app element exists.
const HEATER_TAG = "splashme-heater-card";
function registerHeaterCard() {
  if (!window.customElements.get(HEATER_TAG)) {
    window.customElements.define(HEATER_TAG, SplashmeHeaterCard);
  }
  window.customCards = window.customCards || [];
  if (!window.customCards.some((card) => card.type === HEATER_TAG)) {
    window.customCards.push({
      type: HEATER_TAG,
      name: "SplashMe Heater Card",
      description: "Target temperature dial with the heating sources to use, per pool / spa tab",
    });
  }
}
customElements.whenDefined("home-assistant").then(registerHeaterCard);
