/**
 * SplashMe status cards — the app's dashboard tiles for Home Assistant.
 *
 *   type: custom:splashme-chemistry-card
 *   stable_entity: binary_sensor.chemistry_stable
 *   pump_entity: switch.filter_pump        # optional: "Pump off" / "Waiting for flow" while not stable
 *   orp_entity: sensor.orp                 orp_target_entity: number.target_orp
 *   orp_dosing_entity: binary_sensor.chlorine_dosing   # running dot
 *   orp_enable_entity: switch.chlorinator_enabled        # enable check (optional)
 *   ph_entity: sensor.ph                   ph_target_entity: number.target_ph
 *   ph_dosing_entity: binary_sensor.ph_dosing
 *   ph_enable_entity: switch.ph_dosing_enabled
 *
 *   type: custom:splashme-filtration-card
 *   speed_entity: sensor.pump_speed        flow_entity: sensor.flow_rate
 *   # no speed_entity (single speed pump): the ring shows the pump on/off
 *   pressure_entity: sensor.pressure       pressure_max: 200        # kPa, bar full scale
 *   type_entity: sensor.pump_brand         mode_entity: sensor.pump_mode
 *   pump_entity: switch.filter_pump        # optional, ring toggles it
 *   linked:                                # devices that run with the pump
 *     - name: Ozone
 *       entity: switch.ozone
 *
 * Bundled with the splashme integration; registered automatically.
 */

const GAUGE_START = 225; // degrees clockwise from 12 o'clock, gap at the bottom
const GAUGE_SWEEP = 270;
const WRITE_DELAY_MS = 800; // coalesce +/- taps into one device write
const PENDING_MS = 15000; // show the requested target until the device confirms

function polar(deg, r) {
  const rad = ((deg - 90) * Math.PI) / 180;
  return { x: 60 + r * Math.cos(rad), y: 60 + r * Math.sin(rad) };
}

function arcPath(from, to, r) {
  if (to - from < 0.01) return "";
  if (to - from < 2) to = from + 2; // keep an on-target reading visible as a dot
  const a = polar(from, r);
  const b = polar(to, r);
  return `M ${a.x} ${a.y} A ${r} ${r} 0 ${to - from > 180 ? 1 : 0} 1 ${b.x} ${b.y}`;
}

class SplashmeCardBase extends HTMLElement {
  set hass(hass) {
    this._hass = hass;
    if (!this._built) {
      this.attachShadow({ mode: "open" });
      this.shadowRoot.innerHTML = this._template();
      this._built = true;
    }
    this._update();
  }

  _state(entityId) {
    if (!entityId || !this._hass) return null;
    const st = this._hass.states[entityId];
    return !st || st.state === "unavailable" || st.state === "unknown" ? null : st;
  }

  _number(entityId) {
    const st = this._state(entityId);
    const v = st ? parseFloat(st.state) : NaN;
    return Number.isFinite(v) ? v : null;
  }

  _attr(entityId, name, fallback) {
    const st = this._state(entityId);
    const v = st && st.attributes ? st.attributes[name] : undefined;
    return Number.isFinite(v) ? v : fallback;
  }

  getCardSize() {
    return 4;
  }
}

const BASE_STYLE = `
  :host { display: block; }
  ha-card { padding: 16px; }
  .title { font-size: 18px; font-weight: 500; color: var(--secondary-text-color); }
  .dot { display: inline-block; width: 12px; height: 12px; border-radius: 50%; background: var(--disabled-color, #9e9e9e); }
  .dot.on { background: #4caf50; box-shadow: 0 0 6px #4caf50; }
  .muted { color: var(--secondary-text-color); }
`;

// ---------------------------------------------------------------------------
// Chemistry
// ---------------------------------------------------------------------------

/**
 * Status line from the Chemistry Stable and filter pump states. While not
 * stable, pH/ORP hold the reading from when the pump last ran: say why.
 */
function chemistryStatus(stable, pump) {
  if (!stable) return { text: "--", cls: "" };
  if (stable.state === "on") return { text: "Stable", cls: "stable" };
  if (!pump) return { text: "Last reading", cls: "held" };
  return { text: pump.state === "off" ? "Pump off" : "Waiting for flow", cls: "held" };
}

class SplashmeChemistryCard extends SplashmeCardBase {
  setConfig(config) {
    if (!config.ph_entity && !config.orp_entity) {
      throw new Error("splashme-chemistry-card: 'ph_entity' or 'orp_entity' is required");
    }
    this._config = config;
    this._built = false;
    this._pending = {}; // target entity -> {value, until}
    this._timers = {};
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._built) {
      this.attachShadow({ mode: "open" });
      this.shadowRoot.innerHTML = this._template();
      this.shadowRoot.addEventListener("click", (ev) => {
        const enable = ev.target.closest("button.enable");
        if (enable && !enable.disabled) {
          this._toggleEnable(enable.closest(".gauge").dataset.key);
          return;
        }
        const btn = ev.target.closest("button.step");
        if (!btn || btn.disabled) return;
        this._stepTarget(btn.closest(".gauge").dataset.key, btn.classList.contains("plus") ? 1 : -1);
      });
      this._built = true;
    }
    this._update();
  }

  _targetSpec(key) {
    const cfg = this._config;
    return key === "orp"
      ? { entity: cfg.orp_target_entity, min: 400, max: 900, step: 10, digits: 0 }
      : { entity: cfg.ph_target_entity, min: 6.5, max: 8.0, step: 0.1, digits: 1 };
  }

  /** Target as shown: the requested value while a write is in flight. */
  _target(key) {
    const spec = this._targetSpec(key);
    const st = this._state(spec.entity);
    const actual = st ? parseFloat(st.state) : NaN;
    const p = this._pending[spec.entity];
    if (p) {
      if (actual === p.value) delete this._pending[spec.entity];
      else if (Date.now() < p.until) return p.value;
      else delete this._pending[spec.entity];
    }
    return Number.isFinite(actual) ? actual : null;
  }

  _toggleEnable(key) {
    const entity = key === "orp" ? this._config.orp_enable_entity : this._config.ph_enable_entity;
    const st = this._state(entity);
    if (!st) return;
    this._hass.callService("switch", st.state === "on" ? "turn_off" : "turn_on", { entity_id: entity });
  }

  _stepTarget(key, direction) {
    const spec = this._targetSpec(key);
    const current = this._target(key);
    if (current == null) return;
    const step = this._attr(spec.entity, "step", spec.step);
    const min = this._attr(spec.entity, "min", spec.min);
    const max = this._attr(spec.entity, "max", spec.max);
    const value = Number((Math.max(min, Math.min(max, current + direction * step))).toFixed(spec.digits));
    this._pending[spec.entity] = { value, until: Date.now() + PENDING_MS };
    this._update();
    clearTimeout(this._timers[spec.entity]);
    this._timers[spec.entity] = setTimeout(() => {
      this._hass.callService("number", "set_value", { entity_id: spec.entity, value });
    }, WRITE_DELAY_MS);
  }

  _template() {
    const gauge = (key) => `
      <div class="gauge" data-key="${key}">
        <svg viewBox="0 0 120 120">
          <path class="track" d="${arcPath(GAUGE_START, GAUGE_START + GAUGE_SWEEP, 48)}"></path>
          <path class="fill"></path>
          <line class="target" stroke-width="3"></line>
          <text class="caption" x="60" y="52">target</text>
          <text class="value" x="60" y="72">--</text>
          <text class="caption last" x="60" y="99" visibility="hidden">last reading</text>
          <circle class="reading-mark" r="6"></circle>
          <text class="reading" x="60" y="112">--</text>
        </svg>
        <div class="legend"><span class="name"></span> <span class="dot"></span></div>
        <button class="enable" title="Turn this dosing function on or off"><span class="pip"></span><span class="enable-text">Dosing</span></button>
        <div class="target-row">
          <button class="step minus" aria-label="Lower target">−</button>
          <span class="target-value">adjust target</span>
          <button class="step plus" aria-label="Raise target">+</button>
        </div>
      </div>`;
    return `
      <style>
        ${BASE_STYLE}
        .status { font-size: 22px; font-weight: 600; margin: 2px 0 6px; }
        .status.stable { color: #4caf50; }
        .status.held { color: var(--secondary-text-color); }
        .gauges { display: flex; justify-content: space-around; gap: 8px; }
        .gauge { flex: 1; max-width: 170px; text-align: center; }
        svg { width: 100%; display: block; }
        .track { fill: none; stroke: var(--divider-color, #9e9e9e); stroke-width: 8; stroke-linecap: round; }
        .fill { fill: none; stroke-width: 8; stroke-linecap: round; }
        .target { stroke: var(--primary-text-color); opacity: 0.6; }
        .caption { font-size: 9px; fill: var(--secondary-text-color); text-anchor: middle; letter-spacing: 0.5px; }
        .value { font-size: 20px; font-weight: 600; fill: var(--primary-text-color); text-anchor: middle; }
        .reading { font-size: 15px; font-weight: 700; text-anchor: middle; }
        .reading-mark { stroke: var(--card-background-color, #fff); stroke-width: 2; }
        .legend { font-size: 17px; font-weight: 600; margin-top: -8px; display: flex; align-items: center; justify-content: center; gap: 8px; }
        .legend .name { color: #4caf50; }
        .legend .dot[hidden] { display: none; }
        button.enable { display: inline-flex; align-items: center; gap: 7px; margin-top: 6px; padding: 4px 12px 4px 8px; border-radius: 14px; border: 1px solid var(--divider-color, #9e9e9e); background: none; color: var(--secondary-text-color); font: inherit; font-size: 13px; cursor: pointer; }
        button.enable .pip { width: 10px; height: 10px; border-radius: 50%; background: var(--disabled-color, #9e9e9e); }
        button.enable.on { border-color: #4caf50; color: #2e7d32; background: rgba(76, 175, 80, 0.12); }
        button.enable.on .pip { background: #4caf50; }
        button.enable[hidden] { display: none; }
        button.enable:disabled { opacity: 0.4; cursor: default; }
        /* Dosing switched off: the whole gauge goes grey so it reads as inactive. */
        .gauge.off svg { filter: grayscale(1); opacity: 0.45; }
        .gauge.off .legend .name { color: var(--secondary-text-color); }
        .gauge.off .dot.on { background: var(--disabled-color, #9e9e9e); box-shadow: none; }
        .gauge.off .target-row { opacity: 0.5; }
        .target-row { display: flex; align-items: center; justify-content: center; gap: 10px; margin-top: 8px; }
        .target-row[hidden] { display: none; }
        .target-value { font-size: 12px; color: var(--secondary-text-color); min-width: 72px; text-align: center; }
        button.step { width: 32px; height: 32px; border-radius: 50%; border: 2px solid #4dd0c4; background: none; color: #4dd0c4; font-size: 20px; line-height: 1; cursor: pointer; }
        button.step:disabled { opacity: 0.4; cursor: default; }
      </style>
      <ha-card>
        <div class="title">Chemistry</div>
        <div class="status">--</div>
        <div class="gauges">${gauge("orp")}${gauge("ph")}</div>
      </ha-card>`;
  }

  _update() {
    if (!this._built) return;
    const root = this.shadowRoot;
    const cfg = this._config;
    const status = root.querySelector(".status");
    const { text, cls } = chemistryStatus(this._state(cfg.stable_entity), this._state(cfg.pump_entity));
    status.textContent = text;
    status.className = `status ${cls}`;
    const held = cls === "held";

    this._gauge("orp", {
      name: "ORP",
      value: this._number(cfg.orp_entity),
      target: this._target("orp"),
      target_entity: cfg.orp_target_entity,
      min: this._attr(cfg.orp_target_entity, "min", 400),
      max: this._attr(cfg.orp_target_entity, "max", 900),
      good: 50, // mV either side of the target that still reads green
      digits: 0,
      dosing: this._state(cfg.orp_dosing_entity),
      dosing_entity: cfg.orp_dosing_entity,
      enable_entity: cfg.orp_enable_entity,
      hidden: !cfg.orp_entity,
      held,
    });
    this._gauge("ph", {
      name: "pH",
      value: this._number(cfg.ph_entity),
      target: this._target("ph"),
      target_entity: cfg.ph_target_entity,
      min: this._attr(cfg.ph_target_entity, "min", 6.5),
      max: this._attr(cfg.ph_target_entity, "max", 8.0),
      good: 0.2,
      digits: 1,
      dosing: this._state(cfg.ph_dosing_entity),
      dosing_entity: cfg.ph_dosing_entity,
      enable_entity: cfg.ph_enable_entity,
      hidden: !cfg.ph_entity,
      held,
    });
  }

  /** Colour for a deviation: green inside the good band, then yellow deepening to red at the span. */
  static deviationColor(off, good, span) {
    if (off <= good) return "#4caf50";
    const t = Math.min(1, (off - good) / Math.max(span - good, 1e-9));
    return `hsl(${Math.round(60 * (1 - t))}, 92%, ${Math.round(50 - 8 * t)}%)`;
  }

  _gauge(key, g) {
    const el = this.shadowRoot.querySelector(`.gauge[data-key="${key}"]`);
    el.hidden = g.hidden;
    if (g.hidden) return;
    // Like the app: the gauge runs 0 .. 2 x target so the target sits at the
    // top centre, the fill runs from the gauge start to the reading, and its
    // colour deepens from green through yellow to red as the reading drifts.
    const centre = GAUGE_START + GAUGE_SWEEP / 2;
    const fill = el.querySelector(".fill");
    const tick = el.querySelector(".target");
    const mark = el.querySelector(".reading-mark");
    const reading = el.querySelector(".reading");
    const hide = (node) => node.setAttribute("visibility", "hidden");
    const show = (node) => node.removeAttribute("visibility");
    // Not stable: the number is the reading from when the pump last ran.
    (g.held && g.value != null ? show : hide)(el.querySelector(".last"));
    if (g.value == null || g.target == null) {
      fill.setAttribute("d", "");
      hide(tick); hide(mark);
      reading.textContent = g.value == null ? "--" : g.value.toFixed(g.digits);
      reading.style.fill = "var(--secondary-text-color)";
    } else {
      const span = Math.max(g.target, 1e-9);
      const dev = Math.max(-1, Math.min(1, (g.value - g.target) / span));
      const at = centre + (GAUGE_SWEEP / 2) * dev;
      const colour = SplashmeChemistryCard.deviationColor(Math.abs(g.value - g.target), g.good, span);
      fill.setAttribute("d", arcPath(GAUGE_START, at, 48));
      fill.style.stroke = colour;
      const a = polar(centre, 40);
      const b = polar(centre, 56);
      tick.setAttribute("x1", a.x); tick.setAttribute("y1", a.y);
      tick.setAttribute("x2", b.x); tick.setAttribute("y2", b.y);
      show(tick);
      const m = polar(at, 48);
      mark.setAttribute("cx", m.x); mark.setAttribute("cy", m.y);
      mark.style.fill = colour;
      show(mark);
      reading.textContent = g.value.toFixed(g.digits);
      reading.style.fill = colour;
    }
    el.querySelector(".value").textContent = g.target == null ? "--" : g.target.toFixed(g.digits);
    el.querySelector(".name").textContent = g.name;
    el.querySelector(".dot").hidden = !g.dosing_entity; // no doser fitted: no dosing light
    el.querySelector(".dot").className = `dot ${g.dosing && g.dosing.state === "on" ? "on" : ""}`;
    el.querySelector(".dot").title = g.dosing ? `${g.name} dosing ${g.dosing.state === "on" ? "running" : "idle"}` : "";
    const button = el.querySelector("button.enable");
    button.hidden = !g.enable_entity;
    const enable = this._state(g.enable_entity);
    const enabled = !!enable && enable.state === "on";
    button.disabled = !enable;
    button.className = `enable ${enabled ? "on" : ""}`;
    button.querySelector(".enable-text").textContent = enable ? `Dosing ${enabled ? "ON" : "OFF"}` : "Dosing";
    el.classList.toggle("off", !!enable && !enabled);
    const row = el.querySelector(".target-row");
    row.hidden = !g.target_entity;
    row.querySelectorAll("button").forEach((b) => (b.disabled = g.target == null));
  }
}

// ---------------------------------------------------------------------------
// Filtration
// ---------------------------------------------------------------------------

class SplashmeFiltrationCard extends SplashmeCardBase {
  setConfig(config) {
    if (!config.speed_entity && !config.flow_entity) {
      throw new Error("splashme-filtration-card: 'speed_entity' or 'flow_entity' is required");
    }
    this._config = { pressure_max: 200, linked: [], ...config };
    this._built = false;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._built) {
      this.attachShadow({ mode: "open" });
      this.shadowRoot.innerHTML = this._template();
      this.shadowRoot.addEventListener("click", (ev) => {
        const chip = ev.target.closest(".chip");
        if (chip && !chip.classList.contains("unavailable")) this._toggle(chip.dataset.entity);
        else if (ev.target.closest(".ring") && this._config.pump_entity) this._toggle(this._config.pump_entity);
      });
      this._built = true;
    }
    this._update();
  }

  _toggle(entityId) {
    const st = this._state(entityId);
    if (!st) return;
    this._hass.callService("switch", st.state === "on" ? "turn_off" : "turn_on", { entity_id: entityId });
  }

  _template() {
    return `
      <style>
        ${BASE_STYLE}
        .top { display: grid; grid-template-columns: 1fr 1fr; align-items: center; margin-top: 8px; }
        .ring { max-width: 130px; margin: 0 auto; }
        svg { width: 100%; display: block; }
        .ring-track { fill: none; stroke: var(--divider-color, #9e9e9e); stroke-width: 8; }
        .ring-fill { fill: none; stroke: #4dd0c4; stroke-width: 8; stroke-linecap: round; transform: rotate(-90deg); transform-origin: 60px 60px; transition: stroke-dasharray 0.6s; }
        .ring-text { font-size: 18px; fill: var(--primary-text-color); text-anchor: middle; }
        .flow { text-align: center; border-left: 1px solid var(--divider-color, #9e9e9e); }
        .flow ha-icon { --mdc-icon-size: 30px; color: #4dd0c4; }
        .flow b { display: block; font-size: 26px; font-weight: 600; }
        .bar-row { display: flex; align-items: center; gap: 12px; margin-top: 14px; font-size: 17px; }
        .bar { flex: 1; height: 12px; border-radius: 6px; background: var(--divider-color, #9e9e9e); overflow: hidden; }
        .bar div { height: 100%; width: 0; background: #4caf50; border-radius: 6px; transition: width 0.6s; }
        .bar div.high { background: #ff9800; }
        .foot { display: flex; justify-content: space-between; margin-top: 12px; font-size: 14px; }
        .ring.clickable { cursor: pointer; }
        .ring-fill.on { stroke: #4caf50; }
        .linked { margin-top: 14px; }
        .linked[hidden] { display: none; }
        .linked .caption { font-size: 12px; color: var(--secondary-text-color); display: flex; align-items: center; gap: 4px; margin-bottom: 6px; }
        .linked .caption ha-icon { --mdc-icon-size: 16px; }
        .chips { display: flex; flex-wrap: wrap; gap: 8px; }
        .chip { display: inline-flex; align-items: center; gap: 6px; padding: 5px 10px; border-radius: 14px; border: 1px solid var(--divider-color, #9e9e9e); font-size: 13px; cursor: pointer; }
        .chip.unavailable { opacity: 0.4; cursor: default; }
        .chip.on { border-color: #4caf50; }
      </style>
      <ha-card>
        <div class="title">Filtration</div>
        <div class="top">
          <div class="ring">
            <svg viewBox="0 0 120 120">
              <circle class="ring-track" cx="60" cy="60" r="50"></circle>
              <circle class="ring-fill" cx="60" cy="60" r="50"></circle>
              <text class="ring-text" x="60" y="66">--</text>
            </svg>
          </div>
          <div class="flow"><ha-icon icon="mdi:weather-windy"></ha-icon><b>--</b></div>
        </div>
        <div class="bar-row"><span class="muted">Filter</span><div class="bar"><div></div></div><span class="pressure muted"></span></div>
        <div class="foot"><span class="type muted"></span><span class="mode muted"></span></div>
        <div class="linked">
          <div class="caption"><ha-icon icon="mdi:link-variant"></ha-icon>Runs with the pump</div>
          <div class="chips"></div>
        </div>
      </ha-card>`;
  }

  _update() {
    if (!this._built) return;
    const root = this.shadowRoot;
    const cfg = this._config;
    const pump = this._state(cfg.pump_entity);
    // No speed_entity: a single speed pump (only ever 0 or 100 %), so the ring shows on/off.
    const onOff = !cfg.speed_entity;
    const speed = onOff ? (pump ? (pump.state === "on" ? 100 : 0) : null) : this._number(cfg.speed_entity);
    const circumference = 2 * Math.PI * 50;
    const fill = root.querySelector(".ring-fill");
    const pct = speed == null ? 0 : Math.max(0, Math.min(100, speed));
    fill.setAttribute("stroke-dasharray", `${(circumference * pct) / 100} ${circumference}`);
    fill.setAttribute("visibility", pct > 0 ? "visible" : "hidden"); // a 0-length round cap still draws a dot
    root.querySelector(".ring-text").textContent =
      speed == null ? "--" : onOff ? (speed ? "ON" : "OFF") : `${Math.round(speed)} %`;

    const flow = this._number(cfg.flow_entity);
    root.querySelector(".flow b").textContent = flow == null ? "--" : `${Math.round(flow)} lpm`;
    root.querySelector(".flow").hidden = !cfg.flow_entity;

    const pressure = this._number(cfg.pressure_entity);
    const barRow = root.querySelector(".bar-row");
    barRow.hidden = !cfg.pressure_entity;
    const frac = pressure == null ? 0 : Math.max(0, Math.min(1, pressure / cfg.pressure_max));
    const bar = root.querySelector(".bar div");
    bar.style.width = `${frac * 100}%`;
    bar.className = frac > 0.8 ? "high" : "";
    root.querySelector(".pressure").textContent = pressure == null ? "" : `${pressure.toFixed(0)} kPa`;

    const type = this._state(cfg.type_entity);
    const mode = this._state(cfg.mode_entity);
    root.querySelector(".type").textContent = type ? type.state : "";
    root.querySelector(".mode").textContent = mode ? mode.state : "";

    root.querySelector(".ring").classList.toggle("clickable", !!pump);
    fill.classList.toggle("on", !!pump && pump.state === "on");

    const linked = root.querySelector(".linked");
    linked.hidden = !cfg.linked.length;
    root.querySelector(".chips").innerHTML = cfg.linked
      .map((d) => {
        const st = this._state(d.entity);
        const cls = st ? (st.state === "on" ? "on" : "") : "unavailable";
        return `<span class="chip ${cls}" data-entity="${d.entity}"><span class="dot ${st && st.state === "on" ? "on" : ""}"></span>${d.name}</span>`;
      })
      .join("");
  }
}

// See splashme-tank-card.js for why registration waits for the app element.
function registerStatusCards() {
  const cards = [
    [SplashmeChemistryCard, "splashme-chemistry-card", "SplashMe Chemistry Card", "ORP and pH gauges with dosing status"],
    [SplashmeFiltrationCard, "splashme-filtration-card", "SplashMe Filtration Card", "Pump speed, flow and filter pressure"],
  ];
  window.customCards = window.customCards || [];
  for (const [cls, tag, name, description] of cards) {
    if (!window.customElements.get(tag)) window.customElements.define(tag, cls);
    if (!window.customCards.some((card) => card.type === tag)) {
      window.customCards.push({ type: tag, name, description });
    }
  }
}
customElements.whenDefined("home-assistant").then(registerStatusCards);
