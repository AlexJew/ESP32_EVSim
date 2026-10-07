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
const VOLT_SERIES = [
  { id: "car", key: "charger_v", label: "Car charger", color: "#c27b14" },
  { id: "dc", key: "dc_v", label: "DC input", color: "#2a5fa8" },
  { id: "ac", key: "ac_v", label: "AC output", color: "#7b4fa3" },
];
const WINDOW_S = 600;
const MIN_SPAN_S = 20;      // the furthest the chart zooms in
const MARKER_SPAN_S = 60;   // zoomed to this span or narrower, every 1 s frame gets a marker
const PLOT = { left: 58, right: 12, top: 12, bottom: 28 };   // margins around the plot, in CSS pixels
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

// The same for volts: 100 V steps, never lower than 300 V so the 230 V mains and the 520 V DC side both fit.
function axisVolts(value) {
  return { top: Math.max(300, Math.ceil(value / 100) * 100), step: 100 };
}

// A zoom is {from, to}, in epoch seconds, or null: the whole window, the last WINDOW_S ending at nowS (follow live).
// clampView keeps a zoom of 20 s to 10 min inside the history [nowS - WINDOW_S, nowS]; a frozen zoom that has scrolled
// out of the history is pinned to its oldest edge.
function clampView(zoom, nowS) {
  const span = Math.min(WINDOW_S, Math.max(MIN_SPAN_S, zoom.to - zoom.from));
  let from = Math.max(zoom.from, nowS - WINDOW_S);
  if (from + span > nowS) from = nowS - span;
  return { from, to: from + span };
}

// Zoom around `anchor` (an epoch second) by `factor` (below 1 zooms in); the anchor keeps its place on screen.
// Returns the new zoom, or null once the whole window is shown again (follow live).
function zoomView(zoom, anchor, factor, nowS) {
  const current = zoom || { from: nowS - WINDOW_S, to: nowS };
  const span = Math.min(WINDOW_S, Math.max(MIN_SPAN_S, (current.to - current.from) * factor));
  if (span >= WINDOW_S) return null;
  const ratio = (anchor - current.from) / (current.to - current.from);
  const from = anchor - ratio * span;
  return clampView({ from, to: from + span }, nowS);
}

// Move a zoomed window by `dtS` seconds (positive: later). The whole window cannot move.
function panView(zoom, dtS, nowS) {
  if (zoom === null) return null;
  return clampView({ from: zoom.from + dtS, to: zoom.to + dtS }, nowS);
}

// Round-numbered time ticks for a zoomed span: about six of them, on multiples of the step.
function timeTicks(from, to) {
  const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600];
  const target = (to - from) / 6;
  const step = steps.find((candidate) => candidate >= target) || 600;
  const ticks = [];
  for (let t = Math.ceil(from / step) * step; t <= to; t += step) ticks.push(t);
  return { step, ticks };
}

function clockLabel(t, step) {
  const date = new Date(t * 1000);
  const two = (n) => String(n).padStart(2, "0");
  const hm = `${two(date.getHours())}:${two(date.getMinutes())}`;
  return step < 60 ? `${hm}:${two(date.getSeconds())}` : hm;
}

// `zoom` is null for the whole 10-minute window, drawn exactly as before; a zoom adds clock-time ticks, a vertical scale
// that follows what is in view, markers on the 1 s frames when narrow, and clips the lines to the plot.
function drawChart(canvas, frames, visible, nowS, view = POWER_VIEW, zoom = null) {
  const ratio = window.devicePixelRatio || 1;
  const width = canvas.clientWidth || 860;
  const height = canvas.clientHeight || 300;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  ctx.clearRect(0, 0, width, height);

  const { left, right, top, bottom } = PLOT;
  const plotW = width - left - right;
  const plotH = height - top - bottom;
  const range = zoom ? clampView(zoom, nowS) : { from: nowS - WINDOW_S, to: nowS };
  const span = range.to - range.from;
  const shown = zoom ? frames.filter((frame) => frame.t >= range.from - 1 && frame.t <= range.to + 1) : frames;
  let highest = 0;
  for (const series of view.series) {
    if (!visible.has(seriesId(series))) continue;
    for (const frame of shown) {
      if (typeof frame[series.key] === "number" && frame[series.key] > highest) highest = frame[series.key];
    }
  }
  const scale = ({ W: axis, A: axisAmps, V: axisVolts }[view.unit] || axis)(highest);
  const x = (t) => left + ((t - range.from) / span) * plotW;
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
  if (zoom) {
    const { step, ticks } = timeTicks(range.from, range.to);
    for (const t of ticks) {
      const tx = x(t);
      ctx.strokeStyle = GRID;
      ctx.beginPath();
      ctx.moveTo(tx, top);
      ctx.lineTo(tx, top + plotH);
      ctx.stroke();
      ctx.fillStyle = MUTED;
      ctx.textAlign = tx < left + 24 ? "left" : tx > left + plotW - 24 ? "right" : "center";
      ctx.fillText(clockLabel(t, step), tx, top + plotH + 8);
    }
  } else {
    for (const [seconds, label, align] of [[-WINDOW_S, "−10 min", "left"], [-WINDOW_S / 2, "−5 min", "center"], [0, "now", "right"]]) {
      ctx.textAlign = align;
      ctx.fillText(label, x(nowS + seconds), top + plotH + 8);
    }
  }

  if (zoom) {
    ctx.save();
    ctx.beginPath();
    ctx.rect(left, top, plotW, plotH);
    ctx.clip();                                   // zoomed-in lines must not spill into the margins
  }
  ctx.lineWidth = 2;
  ctx.lineJoin = "round";
  for (const series of view.series) {
    if (!visible.has(seriesId(series))) continue;
    ctx.strokeStyle = series.color;
    ctx.fillStyle = series.color;
    for (const run of segments(shown, series.key, view.maxGapS)) {
      if (run.length === 1) {
        ctx.beginPath();
        ctx.arc(x(run[0][0]), y(run[0][1]), 2, 0, 2 * Math.PI);
        ctx.fill();
        continue;
      }
      ctx.beginPath();
      run.forEach(([t, v], index) => (index === 0 ? ctx.moveTo(x(t), y(v)) : ctx.lineTo(x(t), y(v))));
      ctx.stroke();
      if (zoom && span <= MARKER_SPAN_S) {        // the 1 s sampling made visible
        for (const [t, v] of run) {
          ctx.beginPath();
          ctx.arc(x(t), y(v), 2.5, 0, 2 * Math.PI);
          ctx.fill();
        }
      }
    }
  }
  if (zoom) ctx.restore();
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { SERIES, POWER_SERIES, CURRENT_SERIES, VOLT_SERIES, WINDOW_S, MIN_SPAN_S, segments, axis, axisAmps,
                     axisVolts, clampView, zoomView, panView, timeTicks };
}
