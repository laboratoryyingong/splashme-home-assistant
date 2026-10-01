/**
 * SplashMe Tank Card — animated liquid level for dosing drums.
 *
 * Bundled with the splashme integration; registered automatically, no HACS
 * or manual resource setup needed.
 *
 * Example:
 *   type: custom:splashme-tank-card
 *   name: Acid Drum
 *   entity: sensor.pool_controller_acid_remaining
 *   capacity_entity: number.pool_controller_acid_drum_volume
 *   reset_entity: button.pool_controller_reset_acid_volume
 *   color: "#ef6c00"          # optional, default blue
 */

class SplashmeTankCard extends HTMLElement {
  setConfig(config) {
    if (!config.entity) {
      throw new Error("splashme-tank-card: 'entity' is required");
    }
    this._config = config;
    this._built = false;
  }

  set hass(hass) {
    this._hass = hass;
    if (!this._built) {
      this._build();
    }
    this._update();
  }

  _build() {
    const color = this._config.color || "#29b6f6";
    this.attachShadow({ mode: "open" });
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card { padding: 16px; text-align: center; }
        .title { font-size: 16px; font-weight: 500; margin-bottom: 12px; }
        .tank {
          position: relative;
          width: 140px; height: 180px;
          margin: 0 auto;
          border: 3px solid var(--divider-color, #bbb);
          border-radius: 14px 14px 20px 20px;
          overflow: hidden;
          background: var(--card-background-color, #fff);
        }
        .water {
          position: absolute; left: 0; right: 0; bottom: 0;
          height: 0%;
          background: ${color};
          opacity: 0.85;
          transition: height 1.2s ease;
        }
        .wave {
          position: absolute; left: -50%; top: -14px;
          width: 200%; height: 16px;
          fill: ${color};
          animation: drift 4s linear infinite;
        }
        .wave.back {
          top: -10px; opacity: 0.5;
          animation-duration: 6.4s; animation-direction: reverse;
        }
        @keyframes drift {
          from { transform: translateX(0); }
          to   { transform: translateX(25%); }
        }
        .readout {
          position: absolute; inset: 0;
          display: flex; flex-direction: column;
          align-items: center; justify-content: center;
          pointer-events: none;
        }
        .pct { font-size: 30px; font-weight: 600; color: var(--primary-text-color); }
        .vol { font-size: 13px; color: var(--secondary-text-color); }
        .empty-note { font-size: 12px; color: var(--secondary-text-color); margin-top: 8px; }
        button.reset {
          margin-top: 14px;
          padding: 8px 22px;
          border: none; border-radius: 18px;
          background: var(--primary-color, #03a9f4);
          color: var(--text-primary-color, #fff);
          font-size: 14px; cursor: pointer;
        }
        button.reset:disabled { opacity: 0.4; cursor: default; }
      </style>
      <ha-card>
        <div class="title"></div>
        <div class="tank">
          <div class="water">
            <svg class="wave back" viewBox="0 0 200 16" preserveAspectRatio="none">
              <path d="M0 8 Q 12.5 0 25 8 T 50 8 T 75 8 T 100 8 T 125 8 T 150 8 T 175 8 T 200 8 V16 H0 Z"></path>
            </svg>
            <svg class="wave" viewBox="0 0 200 16" preserveAspectRatio="none">
              <path d="M0 8 Q 12.5 16 25 8 T 50 8 T 75 8 T 100 8 T 125 8 T 150 8 T 175 8 T 200 8 V16 H0 Z"></path>
            </svg>
          </div>
          <div class="readout">
            <div class="pct">--%</div>
            <div class="vol"></div>
          </div>
        </div>
        <div class="empty-note" hidden></div>
        <button class="reset">Reset Drum</button>
      </ha-card>
    `;
    this.shadowRoot
      .querySelector("button.reset")
      .addEventListener("click", () => this._reset());
    this._built = true;
  }

  _stateNumber(entityId) {
    if (!entityId || !this._hass) return null;
    const st = this._hass.states[entityId];
    if (!st || st.state === "unavailable" || st.state === "unknown") return null;
    const value = parseFloat(st.state);
    return Number.isFinite(value) ? value : null;
  }

  _update() {
    if (!this._built || !this._hass) return;
    const root = this.shadowRoot;
    const remaining = this._stateNumber(this._config.entity);
    const capacity =
      this._config.capacity != null
        ? Number(this._config.capacity)
        : this._stateNumber(this._config.capacity_entity);

    root.querySelector(".title").textContent = this._config.name || "Drum";

    const note = root.querySelector(".empty-note");
    let pctText = "--%";
    let volText = "";
    let height = 0;

    if (remaining != null && capacity != null && capacity > 0) {
      const pct = Math.max(0, Math.min(100, (remaining / capacity) * 100));
      height = pct;
      pctText = `${Math.round(pct)}%`;
      volText = `${remaining} L / ${capacity} L`;
      note.hidden = true;
    } else if (remaining != null) {
      volText = `${remaining} L`;
      note.textContent = "Set the drum volume to see the fill level";
      note.hidden = false;
    } else {
      note.textContent = "Waiting for data…";
      note.hidden = false;
    }

    root.querySelector(".water").style.height = `${height}%`;
    root.querySelector(".pct").textContent = pctText;
    root.querySelector(".vol").textContent = volText;

    const resetBtn = root.querySelector("button.reset");
    const resetEntity = this._config.reset_entity;
    const resetState = resetEntity ? this._hass.states[resetEntity] : null;
    resetBtn.hidden = !resetEntity;
    resetBtn.disabled = !resetState || resetState.state === "unavailable";
  }

  _reset() {
    if (!this._config.reset_entity || !this._hass) return;
    if (
      !window.confirm(
        "Reset the drum to full? Do this after installing a full drum."
      )
    ) {
      return;
    }
    this._hass.callService("button", "press", {
      entity_id: this._config.reset_entity,
    });
  }

  getCardSize() {
    return 4;
  }
}

// This script is imported from the page head (add_extra_js_url), before the
// Home Assistant bundle runs. HA 2026.8+ then replaces window.customElements
// with a scoped-registry polyfill, and a card defined on the original
// registry is invisible to the new one ("Custom element doesn't exist").
// So register once the app's root element exists, on whichever registry is
// live at that point.
const TAG = "splashme-tank-card";
function registerTankCard() {
  if (!window.customElements.get(TAG)) {
    window.customElements.define(TAG, SplashmeTankCard);
  }
  window.customCards = window.customCards || [];
  if (!window.customCards.some((card) => card.type === TAG)) {
    window.customCards.push({
      type: TAG,
      name: "SplashMe Tank Card",
      description: "Animated dosing drum level with a reset action",
    });
  }
}
customElements.whenDefined("home-assistant").then(registerTankCard);
