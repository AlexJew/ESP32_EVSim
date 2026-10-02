// The power chain and the chart: polls /api/telemetry every second and /api/history every two.
const byId = (id) => document.getElementById(id);
const dashboardState = {
  frames: [],
  visible: new Set(SERIES.map((series) => series.key)),
  busy: { telemetry: false, history: false },
};

function formatWatts(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `${Math.round(value)} W`;
}

function ageText(reading) {
  if (!reading || reading.status === "unavailable") return "No reading yet";
  const stale = reading.status === "stale";
  if (!reading.updated_at) return stale ? "Stale" : "Updated";
  const date = new Date(reading.updated_at);
  const time = Number.isNaN(date.getTime()) ? "unknown time" : date.toLocaleTimeString();
  return `${stale ? "Stale · last updated" : "Updated"} ${time}`;
}

function showWatts(valueId, reading, updatedId) {
  const target = byId(valueId);
  target.textContent = formatWatts(reading && reading.value);
  target.dataset.status = reading ? reading.status : "unavailable";
  if (updatedId) byId(updatedId).textContent = ageText(reading);
}

function renderTelemetry(data) {
  const connected = Boolean(data.mqtt && data.mqtt.connected);
  const badge = byId("mqtt-state");
  badge.dataset.state = connected ? "ok" : "unavailable";
  badge.textContent = connected ? "Broker connected" : "Broker not connected";

  const board = data.board || {};
  showWatts("board-setpoint", board.setpoint_w, "board-updated");
  byId("board-detail").textContent = `${board.status || "no status"} · pilot ${board.state || "—"}`;

  const setpoint = data.setpoint || {};
  const topic = setpoint.topic || "—";
  byId("setpoint-label").textContent = `Setpoint · ${topic}`;
  byId("legend-setpoint").textContent = `Setpoint, average (${topic})`;
  showWatts("setpoint-value", setpoint.setpoint_w, "setpoint-updated");

  const server = data.server || {};
  showWatts("target-value", server.target_dc_w);
  byId("server-detail").textContent = `${setpoint.status || "no status"} · server ${server.mode || "mode unknown"}`;

  showWatts("inverter-dc", data.inverter && data.inverter.dc_w, "inverter-updated");
}

function redrawChart() {
  drawChart(byId("chart"), dashboardState.frames, dashboardState.visible, Date.now() / 1000);
}

function renderHistory(data) {
  dashboardState.frames = Array.isArray(data.frames) ? data.frames : [];
  byId("chart-note").textContent = `${dashboardState.frames.length} s of history · updated ${new Date().toLocaleTimeString()}`;
  redrawChart();
}

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

for (const button of document.querySelectorAll(".legend-item")) {
  button.addEventListener("click", () => {
    const key = button.dataset.series;
    if (dashboardState.visible.has(key)) dashboardState.visible.delete(key);
    else dashboardState.visible.add(key);
    button.setAttribute("aria-pressed", String(dashboardState.visible.has(key)));
    redrawChart();
  });
}
window.addEventListener("resize", redrawChart);
pollTelemetry();
pollHistory();
window.setInterval(pollTelemetry, 1000);
window.setInterval(pollHistory, 2000);
