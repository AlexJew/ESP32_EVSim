// The setpoints, the measurements and the chart: polls /api/telemetry every second and /api/history every two.
// All three charts (power, current, voltage) draw the server's 10-minute history.
const byId = (id) => document.getElementById(id);
const dashboardState = {
  frames: [],
  unit: "W",
  visible: new Set([...POWER_SERIES, ...CURRENT_SERIES, ...VOLT_SERIES].map((series) => series.id)),
  busy: { telemetry: false, history: false },
  stateBusy: false,          // a vehicle-state command is in flight: the poll must not move or enable the dropdown
  boardState: null,          // the state the board presents (A, B or C) once known, else null
};
const VIEWS = {
  W: POWER_VIEW,
  A: { series: CURRENT_SERIES, unit: "A", maxGapS: MAX_GAP_S },
  V: { series: VOLT_SERIES, unit: "V", maxGapS: MAX_GAP_S },
};

function formatWatts(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${Math.round(value)} W`;
}

function formatAmpsValue(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${Number.isInteger(value) ? value : value.toFixed(1)} A`;
}

function formatVolts(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${Math.round(value)} V`;
}

// "11.8 A / 232 V / 8245 W", with a dash for whatever is not measured.
function formatTriple(amps, volts, watts) {
  return `${formatAmpsValue(amps)} / ${formatVolts(volts)} / ${formatWatts(watts)}`;
}

function ageText(reading) {
  if (!reading || reading.status === "unavailable") return "No reading yet";
  const stale = reading.status === "stale";
  if (!reading.updated_at) return stale ? "Stale" : "Updated";
  const date = new Date(reading.updated_at);
  const time = Number.isNaN(date.getTime()) ? "unknown time" : date.toLocaleTimeString();
  return `${stale ? "Stale · last updated" : "Updated"} ${time}`;
}

function readingStatus(reading) {
  return reading ? reading.status : "unavailable";
}

function setValue(id, text, reading) {
  const target = byId(id);
  target.textContent = text;
  target.dataset.status = readingStatus(reading);
}

// The dropdown shows the state the board presents; it is off when the broker is down or the state is unknown,
// and while a command is in flight it is left alone (app.js sets it back when the command ends).
function syncStateSelect(connected, state) {
  const select = byId("vehicle-state");
  const known = connected && (state === "A" || state === "B" || state === "C");
  dashboardState.boardState = known ? state : null;
  select.disabled = !known || dashboardState.stateBusy;
  if (!dashboardState.stateBusy) select.value = known ? state : "";
}

function renderTelemetry(data) {
  const connected = Boolean(data.mqtt && data.mqtt.connected);
  const badge = byId("mqtt-state");
  badge.dataset.state = connected ? "ok" : "unavailable";
  badge.textContent = connected ? "Broker connected" : "Broker not connected";

  const board = data.board || {};
  setValue("board-setpoint", formatWatts(board.setpoint_w && board.setpoint_w.value), board.setpoint_w);
  byId("setpoints-updated").textContent = ageText(board.setpoint_w);
  syncStateSelect(connected, board.state);

  const setpoint = data.setpoint || {};
  setValue("setpoint-value", formatWatts(setpoint.setpoint_w && setpoint.setpoint_w.value), setpoint.setpoint_w);
  byId("setpoint-value").title = setpoint.topic ? `Topic ${setpoint.topic}` : "";

  const inverter = data.inverter || {};
  const value = (name) => (inverter[name] ? inverter[name].value : null);
  setValue("inverter-dc", formatTriple(value("dc_a"), value("dc_v"), value("dc_w")), inverter.dc_w);
  setValue("inverter-ac", formatTriple(value("ac_a"), value("ac_v"), value("ac_w")), inverter.ac_w);
  byId("measurements-updated").textContent = `Inverter · ${ageText(inverter.dc_w)}`;
}

// -- the chart ---------------------------------------------------------------------------------------

function currentView() {
  return VIEWS[dashboardState.unit];
}

function updateLegendAvailability() {
  const frames = dashboardState.frames;
  for (const button of document.querySelectorAll(".legend-item")) {
    const series = currentView().series.find((item) => item.id === button.dataset.series);
    const hasData = Boolean(series) && frames.some((frame) => typeof frame[series.key] === "number");
    button.classList.toggle("no-data", !hasData);
    button.title = hasData ? "" : "No data for this line";
  }
}

function buildLegend() {
  const legend = byId("legend");
  legend.replaceChildren();
  for (const series of currentView().series) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "legend-item";
    button.dataset.series = series.id;
    button.setAttribute("aria-pressed", String(dashboardState.visible.has(series.id)));
    const swatch = document.createElement("i");
    swatch.className = "swatch";
    swatch.style.background = series.color;
    const label = document.createElement("span");
    label.textContent = series.label;
    button.append(swatch, label);
    button.addEventListener("click", () => {
      if (dashboardState.visible.has(series.id)) dashboardState.visible.delete(series.id);
      else dashboardState.visible.add(series.id);
      button.setAttribute("aria-pressed", String(dashboardState.visible.has(series.id)));
      redrawChart();
    });
    legend.append(button);
  }
  updateLegendAvailability();
}

function updateChartNote() {
  byId("chart-note").textContent = `${dashboardState.frames.length} s of history · updated ${new Date().toLocaleTimeString()}`;
}

function redrawChart() {
  drawChart(byId("chart"), dashboardState.frames, dashboardState.visible, Date.now() / 1000, currentView());
}

function renderHistory(data) {
  dashboardState.frames = Array.isArray(data.frames) ? data.frames : [];
  updateChartNote();
  updateLegendAvailability();
  redrawChart();
}

// -- polling -----------------------------------------------------------------------------------------

async function pollDashboard(kind, path, render, fail) {
  if (dashboardState.busy[kind]) return;
  dashboardState.busy[kind] = true;
  try {
    const response = await fetch(path, { cache: "no-store" });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || `Request failed (${response.status})`);
    render(body);
  } catch (error) {
    fail(error);
  } finally {
    dashboardState.busy[kind] = false;
  }
}

function pollTelemetry() {
  void pollDashboard("telemetry", "/api/telemetry", renderTelemetry, () => {
    const badge = byId("mqtt-state");
    badge.dataset.state = "unavailable";
    badge.textContent = "Dashboard data unavailable";
  });
}

function pollHistory() {
  void pollDashboard("history", "/api/history", renderHistory, () => {
    byId("chart-note").textContent = "History unavailable";
  });
}

byId("chart-unit").addEventListener("change", (event) => {
  dashboardState.unit = ["W", "A", "V"].includes(event.target.value) ? event.target.value : "W";
  buildLegend();
  redrawChart();
});
window.addEventListener("resize", redrawChart);
buildLegend();
pollTelemetry();
pollHistory();
window.setInterval(pollTelemetry, 1000);
window.setInterval(pollHistory, 2000);
