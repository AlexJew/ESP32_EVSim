const el = (id) => document.getElementById(id);
const form = el("limit-form");
const slider = el("current-limit");
let polling = false;
let pollAgain = false;
let writePending = false;
let editing = false;            // the slider is being moved or its value is being sent: the poll must not move it
let queuedValue = null;         // a value chosen while another write was in flight
let installationLimit = null;
let lastSetLimit = null;        // the limit the charger last confirmed

function updateConnection(status, error) {
  const badge = el("connection");
  badge.dataset.state = status;
  const labels = {
    ok: "Charger connected",
    stale: "Showing stale readings",
    fallback_expected: "Charger silent",
    unavailable: "Charger unavailable",
  };
  badge.textContent = labels[status] || "Charger status unknown";
  el("last-updated").textContent = error || "Readings refresh every 2 seconds.";
}

function meanOf(values) {
  const numbers = Object.values(values || {}).filter((value) => Number.isFinite(value));
  return numbers.length ? numbers.reduce((sum, value) => sum + value, 0) / numbers.length : null;
}

// The pill says only "Validated" or "Not validated"; the reason for a refusal is in its tooltip.
function setStatus(kind, text, detail = "") {
  const pill = el("command-status");
  pill.dataset.kind = kind;
  pill.textContent = text;
  pill.title = detail;
}

function tickAt(id, value) {
  const min = Number(slider.min), max = Number(slider.max);
  const tick = el(id);
  tick.style.left = `${((value - min) / (max - min)) * 100}%`;
}

// The big value, and the middle tick that marks it on the slider (hidden when it sits on an end).
function showLimit() {
  const value = Number(slider.value);
  const paused = lastSetLimit === 0 && !editing;
  el("limit-value").textContent = paused ? "0 A" : `${value} A`;
  const min = Number(slider.min), max = Number(slider.max);
  el("tick-min").textContent = `${min} A`;
  el("tick-max").textContent = `${max} A`;
  const mid = el("tick-mid");
  mid.hidden = paused || value <= min || value >= max;
  mid.textContent = `${value} A`;
  if (!mid.hidden) tickAt("tick-mid", value);
}

function updateBanner(status) {
  const banner = el("banner");
  const setLimit = status.set_limit_a ? status.set_limit_a.value : null;
  let text = "";
  let kind = "warning";
  if (status.link === "fallback_expected") {
    text = "No reply from the charger for 30 s or more. It drops to 0 A when its Modbus master goes silent, and puts the limit back on the next request.";
  } else if (status.paused) {
    text = `Charging is paused: the charger signals 0 A although the limit is ${setLimit} A.`;
  } else if (status.restored_at) {
    const age = (Date.now() - new Date(status.restored_at).getTime()) / 1000;
    if (age >= 0 && age < 60) {
      text = "The charger lost its limit and this page put it back.";
      kind = "info";
    }
  }
  banner.hidden = text === "";
  banner.textContent = text;
  banner.dataset.kind = kind;
}

function applyStatus(status) {
  const empty = { value: null, status: "unavailable" };
  const signalled = status.signalled_current_a || empty;
  const meter = status.meter_current_a || empty;
  const voltage = status.meter_voltage_v || empty;
  const power = status.meter_power_w || empty;

  const signalledCell = el("signalled-current");
  signalledCell.textContent = formatAmpsValue(signalled.value);
  signalledCell.dataset.status = signalled.status;
  const chargerCell = el("meter-charger");
  chargerCell.textContent = formatTriple(meanOf(meter.value), meanOf(voltage.value), power.value);
  chargerCell.dataset.status = power.status;
  chargerCell.title = "Current and voltage: mean of the three phases";

  const setLimit = status.set_limit_a || empty;
  const operator = status.operator_limit_a || empty;
  const installation = status.installation_limit_a || empty;
  installationLimit = installation.value;
  el("installation-limit").textContent = `Installation limit: ${installation.value === null ? "—" : `${installation.value} A`}`;
  const warning = el("operator-warning");
  warning.hidden = !status.operator_below_set;
  warning.textContent = status.operator_below_set
    ? `The AMTRON page's limit of ${operator.value} A caps the signalled current.`
    : "";
  updateBanner(status);

  slider.max = installation.value === null ? 16 : Math.min(16, installation.value);
  lastSetLimit = setLimit.value;
  if (setLimit.value !== null && !editing && !writePending) {
    slider.value = Math.max(Number(slider.min), setLimit.value);
  }
  showLimit();
}

async function refresh() {
  if (polling) {
    pollAgain = true;
    return;
  }
  polling = true;
  try {
    const response = await fetch("/api/charger/status", { cache: "no-store" });
    const status = await response.json();
    if (!response.ok) throw new Error(status.error || `Request failed (${response.status})`);
    updateConnection(status.status, status.error);
    applyStatus(status);
  } catch (error) {
    updateConnection("unavailable", error.message);
  } finally {
    polling = false;
    if (pollAgain) {
      pollAgain = false;
      void refresh();
    }
  }
}

function restoreSlider() {
  if (lastSetLimit !== null) slider.value = Math.max(Number(slider.min), lastSetLimit);
  showLimit();
}

async function sendLimit(value) {
  if (writePending) {
    queuedValue = value;
    return;
  }
  if (!Number.isInteger(value) || value < 6 || value > 16 || (installationLimit !== null && value > installationLimit)) {
    setStatus("error", "Not validated", "Choose a whole value from 6 A to the installation limit.");
    editing = false;
    restoreSlider();
    return;
  }
  writePending = true;
  setStatus("pending", "Sending…");
  try {
    const response = await fetch("/api/charger/operator-limit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_limit_a: value }),
    });
    const result = await response.json();
    if (!response.ok || !result.success) throw new Error(result.error || "The charger did not confirm the setting.");
    lastSetLimit = result.set_limit_a;
    setStatus("ok", "Validated");
  } catch (error) {
    setStatus("error", "Not validated", error.message);
    restoreSlider();
  } finally {
    writePending = false;
    if (queuedValue === null) editing = false;
    pollAgain = true;
    await refresh();
    if (queuedValue !== null) {
      const next = queuedValue;
      queuedValue = null;
      await sendLimit(next);
    }
  }
}

// The state dropdown: choosing a letter commands the board; "Validated" means the board accepted it.
const stateSelect = el("vehicle-state");

async function sendState(letter) {
  if (dashboardState.stateBusy || !["A", "B", "C"].includes(letter)) return;
  dashboardState.stateBusy = true;
  stateSelect.disabled = true;
  setStatus("pending", "Sending…");
  try {
    const response = await fetch("/api/vehicle/state", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ state: letter }),
    });
    const result = await response.json();
    if (!response.ok || !result.success) throw new Error(result.error || "The board did not accept the command.");
    setStatus("ok", "Validated");
  } catch (error) {
    setStatus("error", "Not validated", error.message);
    stateSelect.value = dashboardState.boardState || "";      // back to what the board presents
  } finally {
    dashboardState.stateBusy = false;
    stateSelect.disabled = dashboardState.boardState === null;
    pollTelemetry();
  }
}

stateSelect.addEventListener("change", () => void sendState(stateSelect.value));

slider.addEventListener("input", () => {
  editing = true;
  showLimit();
});
// "change" fires when the slider is released (or a key press settles): that is when the value is sent.
slider.addEventListener("change", () => void sendLimit(Number(slider.value)));
form.addEventListener("submit", (event) => event.preventDefault());
el("refresh").addEventListener("click", () => void refresh());
showLimit();
void refresh();
window.setInterval(() => void refresh(), 2000);
