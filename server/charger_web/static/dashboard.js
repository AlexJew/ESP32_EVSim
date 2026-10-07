// The setpoints, the inverter readings and the chart: polls /api/telemetry every second and /api/history every two.
// The current chart has no server history yet, so it keeps its own samples (one per charger poll) since the page opened.
const byId = (id) => document.getElementById(id);
const dashboardState = {
  frames: [],
  currentFrames: [],
  unit: "W",
  visible: new Set([...POWER_SERIES, ...CURRENT_SERIES].map((series) => series.id)),
  busy: { telemetry: false, history: false },
};

function formatWatts(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${Math.round(value)} W`;
}

function formatAmpsValue(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${Number.isInteger(value) ? value : value.toFixed(1)} A`;
}

// "12.8 A / 8857 W", with a dash for whatever is not measured.
function formatPair(amps, watts) {
  return `${formatAmpsValue(amps)} / ${formatWatts(watts)}`;
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

function renderTelemetry(data) {
  const connected = Boolean(data.mqtt && data.mqtt.connected);
  const badge = byId("mqtt-state");
  badge.dataset.state = connected ? "ok" : "unavailable";
  badge.textContent = connected ? "Broker connected" : "Broker not connected";

  const board = data.board || {};
  setValue("board-setpoint", formatWatts(board.setpoint_w && board.setpoint_w.value), board.setpoint_w);
  byId("setpoints-updated").textContent = ageText(board.setpoint_w);

  const setpoint = data.setpoint || {};
  setValue("setpoint-value", formatWatts(setpoint.setpoint_w && setpoint.setpoint_w.value), setpoint.setpoint_w);
  byId("setpoint-value").title = setpoint.topic ? `Topic ${setpoint.topic}` : "";

  const inverter = data.inverter || {};
  setValue("inverter-dc", formatPair(null, inverter.dc_w && inverter.dc_w.value), inverter.dc_w);
  setValue("inverter-ac", formatPair(null, null), null);          // the AC side is not read yet
  byId("measurements-updated").textContent = `Inverter · ${ageText(inverter.dc_w)}`;
}

// -- the chart ---------------------------------------------------------------------------------------

function currentView() {
  return dashboardState.unit === "W"
    ? POWER_VIEW
    : { series: CURRENT_SERIES, unit: "A", maxGapS: 5 };
}

function currentFrames() {
  return dashboardState.unit === "W" ? dashboardState.frames : dashboardState.currentFrames;
}

function updateLegendAvailability() {
  const frames = currentFrames();
  for (const button of document.querySelectorAll(".legend-item")) {
    const series = currentView().series.find((item) => item.id === button.dataset.series);
    const hasData = Boolean(series) && frames.some((frame) => typeof frame[series.key] === "number");
    button.classList.toggle("no-data", !hasData);
    button.title = hasData ? "" : "No data source for this line yet";
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
  const time = new Date().toLocaleTimeString();
  byId("chart-note").textContent = dashboardState.unit === "W"
    ? `${dashboardState.frames.length} s of history · updated ${time}`
    : `Since this page was opened · updated ${time}`;
}

function redrawChart() {
  drawChart(byId("chart"), currentFrames(), dashboardState.visible, Date.now() / 1000, currentView());
}

function renderHistory(data) {
  dashboardState.frames = Array.isArray(data.frames) ? data.frames : [];
  if (dashboardState.unit === "W") {
    updateChartNote();
    updateLegendAvailability();
  }
  redrawChart();
}

// Called by app.js after every charger poll: the signalled current and the mean of the three phases.
function recordCurrentSample(signalled, chargerMeanA) {
  const nowS = Math.floor(Date.now() / 1000);
  const okValue = (reading) => (reading && reading.status === "ok" && Number.isFinite(reading.value) ? reading.value : null);
  const frames = dashboardState.currentFrames;
  frames.push({ t: nowS, sig_a: okValue(signalled), charger_a: chargerMeanA });
  while (frames.length && frames[0].t < nowS - WINDOW_S) frames.shift();
  if (dashboardState.unit === "A") {
    updateChartNote();
    updateLegendAvailability();
    redrawChart();
  }
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
    if (dashboardState.unit === "W") byId("chart-note").textContent = "History unavailable";
  });
}

byId("chart-unit").addEventListener("change", (event) => {
  dashboardState.unit = event.target.value === "A" ? "A" : "W";
  buildLegend();
  updateChartNote();
  redrawChart();
});
window.addEventListener("resize", redrawChart);
buildLegend();
pollTelemetry();
pollHistory();
window.setInterval(pollTelemetry, 1000);
window.setInterval(pollHistory, 2000);
