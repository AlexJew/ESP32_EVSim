// The setpoints, the measurements and the chart: polls /api/telemetry every second and /api/history every two.
// All three charts (power, current, voltage) draw the server's 10-minute history.
const byId = (id) => document.getElementById(id);
const PARTS = ["A", "V", "W"];                           // the bubbles of the Measurements card, in display order
const PART_KEY = "evsim.measurement-parts";

// Which parts of a measurement are shown: the saved choice, or all three. A blocked or unreadable store means all three.
function loadParts() {
  try {
    const stored = JSON.parse(localStorage.getItem(PART_KEY));
    if (Array.isArray(stored) && stored.every((part) => PARTS.includes(part))) return new Set(stored);
  } catch (error) {
    // no storage: show everything
  }
  return new Set(PARTS);
}

function saveParts() {
  try {
    localStorage.setItem(PART_KEY, JSON.stringify(PARTS.filter((part) => dashboardState.parts.has(part))));
  } catch (error) {
    // blocked: the choice simply does not survive a reload
  }
}

const dashboardState = {
  frames: [],
  unit: "W",
  visible: new Set([...POWER_SERIES, ...CURRENT_SERIES, ...VOLT_SERIES].map((series) => series.id)),
  busy: { telemetry: false, history: false },
  zoom: null,                // null follows live; otherwise the zoomed window {from, to}, frozen on absolute times
  parts: loadParts(),        // the bubbles that are on; all three can be off
  measurements: {},          // row -> {amps, volts, watts, reading}: kept so a bubble redraws the rows at once
  stateBusy: false,          // a vehicle-state command is in flight: the poll must not move or enable the dropdown
  boardState: null,          // the state the board presents (A, B or C) once known, else null
  expected: null,            // {letter, until}: the letter the board just accepted, kept until it reports it or 3 s pass
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

// Amps in the measurements always show one decimal (15.0 A), so the column lines up.
function formatAmpsFixed(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${value.toFixed(1)} A`;
}

// "11.8 A / 232 V / 8245 W": only the parts whose bubble is on, a dash for one that is not measured, and a slash only
// between two parts that are shown.
function formatMeasurement(amps, volts, watts) {
  const text = { A: formatAmpsFixed(amps), V: formatVolts(volts), W: formatWatts(watts) };
  return PARTS.filter((part) => dashboardState.parts.has(part)).map((part) => text[part]).join(" / ");
}

const MEASUREMENT_ROWS = { charger: "meter-charger", dc: "inverter-dc", ac: "inverter-ac" };

function drawMeasurement(row) {
  const m = dashboardState.measurements[row];
  if (m) setValue(MEASUREMENT_ROWS[row], formatMeasurement(m.amps, m.volts, m.watts), m.reading);
}

function setMeasurement(row, amps, volts, watts, reading) {
  dashboardState.measurements[row] = { amps, volts, watts, reading };
  drawMeasurement(row);
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
  // The board answers before it changes the state it publishes, so the first polls after "Validated" still carry the
  // old letter: keep the accepted one on screen until the board reports it, or give up after 3 s.
  const expected = dashboardState.expected;
  if (expected && (state === expected.letter || Date.now() > expected.until)) dashboardState.expected = null;
  const shown = dashboardState.expected ? dashboardState.expected.letter : known ? state : "";
  select.disabled = !known || dashboardState.stateBusy;
  if (!dashboardState.stateBusy) select.value = shown;
}

function renderTelemetry(data) {
  const connected = Boolean(data.mqtt && data.mqtt.connected);
  const badge = byId("mqtt-state");
  badge.dataset.state = connected ? "ok" : "unavailable";
  badge.textContent = connected ? "Broker connected" : "Broker not connected";

  const board = data.board || {};
  setValue("board-setpoint", formatWatts(board.setpoint_w && board.setpoint_w.value), board.setpoint_w);
  syncStateSelect(connected, board.state);

  const setpoint = data.setpoint || {};
  setValue("setpoint-value", formatWatts(setpoint.setpoint_w && setpoint.setpoint_w.value), setpoint.setpoint_w);
  byId("setpoint-value").title = setpoint.topic ? `Topic ${setpoint.topic}` : "";

  const inverter = data.inverter || {};
  const value = (name) => (inverter[name] ? inverter[name].value : null);
  setMeasurement("dc", value("dc_a"), value("dc_v"), value("dc_w"), inverter.dc_w);
  setMeasurement("ac", value("ac_a"), value("ac_v"), value("ac_w"), inverter.ac_w);
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

function currentRange(nowS) {
  return dashboardState.zoom ? clampView(dashboardState.zoom, nowS) : { from: nowS - WINDOW_S, to: nowS };
}

function redrawChart() {
  const nowS = Date.now() / 1000;
  drawChart(byId("chart"), dashboardState.frames, dashboardState.visible, nowS, currentView(), dashboardState.zoom);
  const zoomed = dashboardState.zoom !== null;
  const range = currentRange(nowS);
  byId("chart-live").hidden = !zoomed;
  byId("chart-view").hidden = !zoomed;
  byId("chart-view").textContent = zoomed ? `Showing ${Math.round(range.to - range.from)} s` : "";
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

// Zoom the time axis with the wheel or a pinch (a pinch arrives as a wheel event with ctrlKey), pan by dragging, go back
// to live with a double-click or the button. A zoomed window is frozen on absolute times, so new data does not slide it.
const chartCanvas = byId("chart");
let drag = null;

function plotFraction(event) {
  const rect = chartCanvas.getBoundingClientRect();
  const fraction = (event.clientX - rect.left - PLOT.left) / (rect.width - PLOT.left - PLOT.right);
  return Math.min(1, Math.max(0, fraction));
}

function followLive() {
  dashboardState.zoom = null;
  redrawChart();
}

chartCanvas.addEventListener("wheel", (event) => {
  event.preventDefault();
  const nowS = Date.now() / 1000;
  const range = currentRange(nowS);
  const anchor = range.from + plotFraction(event) * (range.to - range.from);
  const delta = event.deltaMode === 1 ? event.deltaY * 16 : event.deltaY;
  const factor = Math.min(2, Math.max(0.5, Math.exp(delta * (event.ctrlKey ? 0.01 : 0.0015))));
  dashboardState.zoom = zoomView(dashboardState.zoom, anchor, factor, nowS);
  redrawChart();
}, { passive: false });
chartCanvas.addEventListener("pointerdown", (event) => {
  if (dashboardState.zoom === null) return;               // the whole window is shown: nothing to pan
  drag = { x: event.clientX, range: currentRange(Date.now() / 1000) };
  if (chartCanvas.setPointerCapture) chartCanvas.setPointerCapture(event.pointerId);
});
chartCanvas.addEventListener("pointermove", (event) => {
  if (drag === null) return;
  const rect = chartCanvas.getBoundingClientRect();
  const secondsPerPixel = (drag.range.to - drag.range.from) / (rect.width - PLOT.left - PLOT.right);
  dashboardState.zoom = panView(drag.range, -(event.clientX - drag.x) * secondsPerPixel, Date.now() / 1000);
  redrawChart();
});
chartCanvas.addEventListener("pointerup", () => { drag = null; });
chartCanvas.addEventListener("pointercancel", () => { drag = null; });
chartCanvas.addEventListener("dblclick", followLive);
byId("chart-live").addEventListener("click", followLive);

for (const part of PARTS) {
  const bubble = byId("part-" + part);
  bubble.setAttribute("aria-pressed", String(dashboardState.parts.has(part)));
  bubble.addEventListener("click", () => {
    if (dashboardState.parts.has(part)) dashboardState.parts.delete(part);
    else dashboardState.parts.add(part);
    bubble.setAttribute("aria-pressed", String(dashboardState.parts.has(part)));
    saveParts();
    for (const row of Object.keys(MEASUREMENT_ROWS)) drawMeasurement(row);
  });
}
buildLegend();
pollTelemetry();
pollHistory();
window.setInterval(pollTelemetry, 1000);
window.setInterval(pollHistory, 2000);
