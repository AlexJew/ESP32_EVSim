// The chart: pure helpers (tested with Node) and the canvas drawing. No library.
// SERIES is the original four-line power chart; the page now draws POWER_SERIES or CURRENT_SERIES.
const SERIES = [
  { key: "cp_w", label: "Board, raw", color: "#8aa699" },
  { key: "setpoint_w", label: "Setpoint, average", color: "#176b4a" },
  { key: "target_dc_w", label: "DC target", color: "#c27b14" },
  { key: "dc_w", label: "Inverter DC power", color: "#2a5fa8" },
];
// `id` names a line across both views (hiding "DC input" hides it in watts and in amps); `key` is the frame field.
// A key that no frame carries yet (ac_w, charger_w, dc_a, ac_a) simply draws nothing.
const POWER_SERIES = [
  { id: "board", key: "cp_w", label: "Board", color: "#8aa699" },
  { id: "ev", key: "setpoint_w", label: "EV setpoint", color: "#176b4a" },
  { id: "car", key: "charger_w", label: "Car charger", color: "#c27b14" },
  { id: "dc", key: "dc_w", label: "DC input", color: "#2a5fa8" },
  { id: "ac", key: "ac_w", label: "AC output", color: "#7b4fa3" },
];
const CURRENT_SERIES = [
  { id: "sig", key: "sig_a", label: "Signalled", color: "#176b4a" },
  { id: "car", key: "charger_a", label: "Car charger", color: "#c27b14" },
  { id: "dc", key: "dc_a", label: "DC input", color: "#2a5fa8" },
  { id: "ac", key: "ac_a", label: "AC output", color: "#7b4fa3" },
];
const WINDOW_S = 600;
const MAX_GAP_S = 2.5;      // frames come once a second; a longer silence is a gap, not a slope
const GRID = "#dce4df";
const MUTED = "#69766f";
const POWER_VIEW = { series: POWER_SERIES, unit: "W", maxGapS: MAX_GAP_S };
const seriesId = (series) => series.id || series.key;

// Runs of consecutive frames that have a number for `key`. A null or missing value, or a missing frame,
// ends a run, so an outage is drawn as a gap.
function segments(frames, key, maxGapS = MAX_GAP_S) {
  const runs = [];
  let run = [];
  for (const frame of frames) {
    const value = frame[key];
    if (typeof value !== "number" || !Number.isFinite(value)) {
      if (run.length) runs.push(run);
      run = [];
      continue;
    }
    if (run.length && frame.t - run[run.length - 1][0] > maxGapS) {
      runs.push(run);
      run = [];
    }
    run.push([frame.t, value]);
  }
  if (run.length) runs.push(run);
  return runs;
}

// The top of the y-axis and the spacing of its grid lines, in watts, for the largest value shown.
function axis(value) {
  const step = value <= 4000 ? 1000 : value <= 10000 ? 2000 : 4000;
  return { top: Math.max(step * 2, Math.ceil(value / step) * step), step };
}

// The same for amps: 4 A steps, never lower than the 16 A the charger can be set to.
function axisAmps(value) {
  return { top: Math.max(16, Math.ceil(value / 4) * 4), step: 4 };
}

function drawChart(canvas, frames, visible, nowS, view = POWER_VIEW) {
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 860;
  const height = canvas.clientHeight || 300;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const left = 58, right = 12, top = 12, bottom = 28;
  const plotW = width - left - right;
  const plotH = height - top - bottom;
  const tMin = nowS - WINDOW_S;
  let highest = 0;
  for (const series of view.series) {
    if (!visible.has(seriesId(series))) continue;
    for (const frame of frames) {
      if (typeof frame[series.key] === "number" && frame[series.key] > highest) highest = frame[series.key];
    }
  }
  const scale = view.unit === "A" ? axisAmps(highest) : axis(highest);
  const x = (t) => left + ((t - tMin) / WINDOW_S) * plotW;
  const y = (v) => top + plotH - (v / scale.top) * plotH;

  ctx.font = "12px system-ui, sans-serif";
  ctx.lineWidth = 1;
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (let v = 0; v <= scale.top; v += scale.step) {
    ctx.strokeStyle = GRID;
    ctx.beginPath();
    ctx.moveTo(left, y(v));
    ctx.lineTo(left + plotW, y(v));
    ctx.stroke();
    ctx.fillStyle = MUTED;
    ctx.fillText(`${v} ${view.unit}`, left - 8, y(v));
  }
  ctx.textBaseline = "top";
  for (const [seconds, label, align] of [[-WINDOW_S, "−10 min", "left"], [-WINDOW_S / 2, "−5 min", "center"], [0, "now", "right"]]) {
    ctx.textAlign = align;
    ctx.fillText(label, x(nowS + seconds), top + plotH + 8);
  }

  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  for (const series of view.series) {
    if (!visible.has(seriesId(series))) continue;
    ctx.strokeStyle = series.color;
    ctx.fillStyle = series.color;
    for (const run of segments(frames, series.key, view.maxGapS)) {
      if (run.length === 1) {
        ctx.beginPath();
        ctx.arc(x(run[0][0]), y(run[0][1]), 2, 0, 2 * Math.PI);
        ctx.fill();
        continue;
      }
      ctx.beginPath();
      run.forEach(([t, v], index) => (index === 0 ? ctx.moveTo(x(t), y(v)) : ctx.lineTo(x(t), y(v))));
      ctx.stroke();
    }
  }
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { SERIES, POWER_SERIES, CURRENT_SERIES, segments, axis, axisAmps };
}
