const el = (id) => document.getElementById(id);
const form = el("limit-form");
const input = el("current-limit");
const applyButton = el("apply");
let polling = false;
let pollAgain = false;
let writePending = false;
let installationLimit = null;

function formatAmps(value) {
  if (value === null || value === undefined || !Number.isFinite(value)) return "Unavailable";
  return `${value} A`;
}

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

function renderReading(target, reading, phases = false) {
  target.replaceChildren();
  if (!reading || reading.status === "unavailable" || reading.value === null || reading.value === undefined) {
    const value = document.createElement("span");
    value.className = "reading-value";
    value.textContent = "Unavailable";
    target.append(value);
    return;
  }
  if (phases && typeof reading.value === "object" && !Array.isArray(reading.value)) {
    for (const [phase, current] of Object.entries(reading.value)) {
      const item = document.createElement("span");
      item.className = "meter-phase";
      const label = document.createElement("small");
      label.textContent = phase;
      item.append(label, document.createTextNode(formatAmps(current)));
      target.append(item);
    }
    if (target.childElementCount === 0) {
      const value = document.createElement("span");
      value.className = "reading-value";
      value.textContent = "Unavailable";
      target.append(value);
    }
    return;
  }
  const value = document.createElement("span");
  value.className = "reading-value";
  value.textContent = formatAmps(reading.value);
  target.append(value);
}

function updateTimestamp(target, reading) {
  if (reading && reading.status === "unavailable") {
    target.textContent = "Reading unavailable";
    return;
  }
  if (!reading || !reading.updated_at) {
    target.textContent = "No successful reading yet";
    return;
  }
  const date = new Date(reading.updated_at);
  const time = Number.isNaN(date.getTime()) ? "unknown time" : date.toLocaleTimeString();
  target.textContent = `${reading.status === "stale" ? "Stale · last updated" : "Updated"} ${time}`;
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
  renderReading(el("signalled-current"), signalled);
  renderReading(el("meter-current"), meter, true);
  updateTimestamp(el("signalled-updated"), signalled);
  updateTimestamp(el("meter-updated"), meter);

  const setLimit = status.set_limit_a || empty;
  const operator = status.operator_limit_a || empty;
  const installation = status.installation_limit_a || empty;
  installationLimit = installation.value;
  el("configured-limit").textContent = formatAmps(setLimit.value);
  el("operator-limit").textContent = formatAmps(operator.value);
  el("installation-limit").textContent = `Installation limit: ${formatAmps(installation.value)}`;
  const warning = el("operator-warning");
  warning.hidden = !status.operator_below_set;
  warning.textContent = status.operator_below_set
    ? `The AMTRON page's own limit of ${operator.value} A caps the signalled current below the ${setLimit.value} A set here.`
    : "";
  el("vehicle-state").textContent = (status.vehicle_state && status.vehicle_state.value) || "Unavailable";
  el("charger-status").textContent = (status.charger_status && status.charger_status.value) || "Unavailable";
  updateTimestamp(el("settings-updated"), setLimit);
  updateBanner(status);
  input.max = installation.value === null ? 16 : Math.min(16, installation.value);
  if (setLimit.value !== null && document.activeElement !== input) input.value = setLimit.value;
  applyButton.disabled = writePending;
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
    el("signalled-updated").textContent = "Refresh failed; last displayed value may be stale";
    el("meter-updated").textContent = "Refresh failed; last displayed value may be stale";
  } finally {
    polling = false;
    if (pollAgain) {
      pollAgain = false;
      void refresh();
    }
  }
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const message = el("write-message");
  const value = Number(input.value);
  message.textContent = "";
  message.dataset.kind = "";
  if (!Number.isInteger(value) || (value !== 0 && (value < 6 || value > 16)) ||
      (installationLimit !== null && value > installationLimit)) {
    message.dataset.kind = "error";
    message.textContent = "Choose 0 A or a whole value from 6 A to the installation limit.";
    return;
  }

  writePending = true;
  applyButton.disabled = true;
  applyButton.textContent = "Applying…";
  try {
    const response = await fetch("/api/charger/operator-limit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ current_limit_a: value }),
    });
    const result = await response.json();
    if (!response.ok || !result.success) throw new Error(result.error || "The charger did not confirm the setting.");
    message.dataset.kind = "success";
    message.textContent = `Confirmed by charger: ${formatAmps(result.set_limit_a)}.`;
    pollAgain = true;
    await refresh();
  } catch (error) {
    message.dataset.kind = "error";
    message.textContent = error.message;
    pollAgain = true;
    await refresh();
  } finally {
    writePending = false;
    applyButton.textContent = "Apply";
    applyButton.disabled = false;
  }
});

el("refresh").addEventListener("click", () => void refresh());
void refresh();
window.setInterval(() => void refresh(), 2000);
