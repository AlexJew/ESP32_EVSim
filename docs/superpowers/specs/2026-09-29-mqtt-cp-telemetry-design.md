# MQTT CP Telemetry and Held Setpoint — Design

**Date:** 2026-09-29
**Status:** awaiting review
**Scope:** publish every CP sample over MQTT, together with a held power setpoint that survives the
charger's periodic gaps and drops to zero when the pilot leaves state C. Smoothing the noisy duty
reading is **not** done on the board; the consumer of the stream does it. Builds on
[2026-09-29-mqtt-state-commands-design.md](2026-09-29-mqtt-state-commands-design.md).

## Purpose

The thesis loop is AMTRON → ESP32 → Fronius inverter. Whatever computes the inverter's power
setpoint needs the CP measurements. Who that is has **not been decided**: a separate server that
subscribes to this stream, a broker-side bridge (the GEN24 is not known to accept an MQTT setpoint;
it offers Modbus TCP and a read-only REST API, see `memory/evsim-inverter-fronius.md`), or the
board itself with a Modbus client added later. This design does not depend on the choice: the
board publishes the same stream in every case. Two properties of the signal shape it:

1. **The charger's offer vanishes for a few seconds** every 6–12 s when nothing draws current
   (`memory/evsim-open-questions.md`). Publishing every sample as a setpoint would send 0 W in each
   gap and make the inverter oscillate. So the board marks each sample and keeps the last valid
   setpoint through a gap.
2. **The measured duty wanders slowly.** In four 60 s captures in state C the duty had a spread of
   about 5–6 points (13–42 %), with a correlation of +0.9 between consecutive samples and +0.4 two
   seconds later. Averaging over 1 s barely helps and publishing one sample per second only
   subsamples the noise. Whether the wander is the charger or an artefact of the interim
   low-voltage reader (see `2026-09-22-low-voltage-cp-reader-design.md`) is not known. The right
   filter depends on that and on what the inverter needs, so it lives with the consumer, where it can be
   changed without reflashing. The board only publishes unfiltered samples.

## Message contract

Topic `evsim/cp`. QoS 0, not retained, one message per 200 ms sample (5 Hz). Nothing is queued while
the client is disconnected; the newest sample simply replaces an unsent older one.

```json
{"t":123456,"freq":1000.0,"duty":24.30,"adc":0,"cp":"PWM_VALID","state":"C",
 "amps":14.58,"watts":10060,"setpoint_w":10060,"status":"live","age_ms":0}
```

| Field | Meaning |
|---|---|
| `t` | Board uptime in ms (`millis()`). There is no wall clock (no NTP), so the consumer stamps arrival time itself. |
| `freq`, `duty`, `adc` | The raw measurement, as in the CSV (`freq_hz`, `duty_pct`, `adc_raw`). |
| `cp` | The CP classification, with the CSV `state` column's names: `NO_SIGNAL`, `DC_12V`, `PWM_VALID`, `OUT_OF_RANGE`. |
| `state` | The pilot state the board presents: `A`, `B` or `C` (the CSV `commanded` column). |
| `amps`, `watts` | From this sample. `null` when `cp` is not `PWM_VALID` or the conversion gives no value; no number is invented. |
| `setpoint_w` | The held setpoint in whole watts, per the status rules. |
| `status` | `live`, `held` or `zero`. |
| `age_ms` | Time since the last valid sample; `null` if there has been none since entering C. |

### Status rules

A sample is **valid** when `cp` is `PWM_VALID` and `watts` is a number.

- **`live`** — state is C and the sample is valid. `setpoint_w` is this sample's watts, `age_ms` is 0.
- **`held`** — state is C, the sample is not valid, a valid one was seen since entering C, and it is
  at most `kSetpointHoldMs` = 30 000 ms old. `setpoint_w` keeps the last valid value.
- **`zero`** — everything else: state A or B; state C with no valid sample since entering C; or the
  last valid sample is more than 30 s old. `setpoint_w` is 0.
- **Leaving state C forgets the held value.** Re-entering C is `zero` until a fresh valid sample
  arrives, so an old value cannot leak into a new session.

The board does no averaging. A consumer should smooth `live` samples only and treat `held` as "no
new information". The hold window is a named constant; changing it means a reflash.

## Design

### Components

- **`lib/cp_setpoint`** — pure C++, natively tested. A step function taking the previous state, "in
  state C", "sample valid", watts and `now_ms`, returning the new state plus the status, setpoint
  watts and age. Elapsed-time arithmetic is unsigned subtraction, correct across the 49-day
  `millis()` wrap.
- **`lib/cp_json`** — pure, natively tested. Formats one sample into the JSON text: `null` for
  missing amps and watts, fixed decimal places, no locale dependence. It returns an error rather
  than a truncated string when the buffer is too small. The buffer is 256 bytes; the worst-case
  message is about 210.
- **`src/net_mqtt`** — gains `net_mqtt_publish_cp(const char *json)`. It writes into a one-slot
  mailbox with `xQueueOverwrite`, which never blocks and keeps only the newest sample. It counts
  each overwrite of an unsent sample; the count appears in the `W` status.
- **The pump task** — serves the existing control queue first (state changes, replies), then the
  mailbox. It wakes every 20 ms instead of 100 ms.
- **`src/main.cpp`** — a few lines in the existing 200 ms sample tick: step the setpoint, format the
  JSON, hand it to `net_mqtt`. The CSV output does not change.

### Data flow

1. The sample tick computes duty, amps and watts as today.
2. It steps `cp_setpoint`, formats the JSON, and writes it into the mailbox.
3. The pump publishes it at QoS 0 while the client is connected, and drops it otherwise.

`handle_command` resets the setpoint state on every pilot-state change, not only in the sample
tick, so a quick C → B → C between two samples cannot keep an old held value alive.

### Why a mailbox, not the control queue

The control queue (8 items) carries state changes and replies, which must not be lost. Five
telemetry messages per second sharing it could push a state publish out. A one-slot mailbox keeps
telemetry off that queue and always holds the latest value.

### Blocking

A QoS 0 publish is sent in the calling task and can wait up to the network timeout when the link
is dead but not yet detected. That delays only the pump task, never `loop()`; control messages
queued behind it wait until the keepalive notices the dead link (about 15 s). This is accepted.

### Failure handling

- Broker or WiFi down: samples are dropped, the CSV continues, the setpoint state keeps updating
  on the board, so the status is right when the link returns.
- Buffer too small for the JSON (not reachable with 256 bytes): the sample is skipped.

## Testing

- **Native unit tests, `cp_setpoint`:** live when in C and valid; held after a gap; the exact
  30 000 ms boundary; zero after expiry; invalid first sample in C; zero outside C; leaving and
  re-entering C forgets the value; clock wraparound.
- **Native unit tests, `cp_json`:** every field, `null` for amps and watts on an invalid sample,
  rounding, truncation reported as an error, NaN and infinity never printed as a bare token.
- **Bench, with `mosquitto_sub -t evsim/cp`:** five messages per second; the status moves through
  `live`, `held` and `zero` as the state and the charger signal change; stopping the broker leaves
  a continuous CSV; the frequency remains exactly 1000 Hz in the jitter capture with the stream
  running. The duty spread cannot be used as an acceptance criterion (see Purpose).

## Out of scope

- Any smoothing or averaging on the board.
- Deciding whether the duty wander is real; a comparison against the charger's REST
  `SignaledCurrentLimit_vehicleif` can be done from the recorded stream later.
- The inverter side, and choosing what consumes the stream. If the board ends up driving the
  inverter directly, the smoothing and a Modbus client become firmware work in a later design; the
  `setpoint_w` and `status` fields are what such code would need.
- Authentication and TLS, as in the state-commands design.
