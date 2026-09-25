# WiFi Station with a Static IP — Design

**Date:** 2026-09-25
**Status:** awaiting review
**Scope:** join the laboratory WLAN as a station on a fixed address, with no DHCP available,
without ever blocking the CP measurement loop. Connectivity only — MQTT and the REST client
are separate, later designs.

## Purpose

The thesis signal chain ([[evsim-tb-overview]]) needs the ESP32 on the same network as the
AMTRON and, later, the inverter: the board publishes a power setpoint over MQTT and subscribes
to pilot-state commands. None of that can be built until the board is reliably on the LAN. This
design delivers that one step and nothing more, so that the network layer is proven before MQTT
adds a second new failure domain on top of it.

The laboratory host runs **no DHCP server**, so the address must be configured on the board.

## Network parameters

| Parameter | Value |
|---|---|
| SSID | `hems` |
| Static IP | `192.168.21.138` |
| Subnet mask | `255.255.255.0` (`/24`) |
| Gateway | `192.168.21.130` (WLAN gateway) |
| DNS | `192.168.21.130` (assumed; unconfirmed) |

### Reachability

`192.168.21.138/24` gives network `192.168.21.0`, host range `192.168.21.1`–`192.168.21.254`.

| Device | Address | Directly reachable |
|---|---|---|
| ESP32 (this board) | `192.168.21.138` | — |
| AMTRON charger | `192.168.21.117` | yes, same `/24` |
| Fronius GEN24 inverter | `192.168.21.122` | yes, same `/24` |
| WLAN gateway | `192.168.21.130` | yes, same `/24` |

**All three devices in the loop share the `/24`.** The project's scope is exactly the loop
AMTRON -> ESP32 -> Fronius, and every hop of it stays inside `192.168.21.0/24`, so none of it
depends on the gateway. A misconfigured or absent gateway therefore cannot break the thesis
signal chain; it only costs off-subnet reachability, which nothing in scope needs.

One consequence worth recording:

- The AMTRON moved from `192.168.188.186` to `192.168.21.117` when it was put on this subnet.
  `tools/charger_poll.py` already tracks the new address; `memory/evsim-amtron-rest-api.md`
  does not and must be corrected.

### There is no internet on this network

No route to the internet exists, so:

- **No NTP, hence no wall-clock time.** The firmware keeps using `millis()` for its CSV
  timestamps, as it does today. Nothing in the design may assume a real date.
- **DNS is unconfirmed and must not be load-bearing.** `kDns` defaults to the gateway on the
  assumption that it relays DNS, but every peer the firmware talks to — the MQTT broker, the
  Fronius inverter, the charger's REST interface — is addressed **by IP, never by hostname**.
  If DNS turns out to be absent, nothing breaks. This is a standing constraint on the MQTT and
  REST designs that follow, not merely a note about this one.

## Non-goals

- **No MQTT.** Chosen deliberately: prove the link, and the ISR-jitter question below, in
  isolation first.
- **No OTA updates.** Flashing stays over USB. OTA competes with the ISR for CPU and would
  muddy the jitter measurement this design exists to settle.
- **No web server, no captive-portal provisioning, no WiFi scanning.** One network, one
  address, both known.
- **No runtime credential storage.** See *Credentials* below.
- **No change to `lib/cp_convert`, `lib/pilot_state`, `src/cp_reader.*` or
  `src/pilot_driver.*`.** The only edit to existing code is `src/main.cpp`: the two new calls
  and the `W` command.

## Credentials

The SSID and password live in **`EVSim/src/wifi_config.h`, committed to git**, at the user's
explicit choice after the alternatives were presented.

**The trade-off, recorded so it is not forgotten:** committing them writes the laboratory
WLAN password permanently into this repository's history. Deleting the file later does not
remove it; only rewriting history (`git filter-repo`) does. This is acceptable while the repo
is private and the credentials are laboratory-local, and it is a deliberate decision, not an
oversight.

**Mitigation by structure:** all six values live in that one file and nothing else reads them
directly. Switching to a gitignored `wifi_config.h` plus a committed `wifi_config.example.h`
is then a `.gitignore` line and a file copy, with no code change anywhere. If this repository
is ever made public, do that first.

## Architecture

The split follows the pattern the codebase already uses ([[evsim-firmware-layout]]): the logic
worth testing goes in `lib/` where the `native` environment can build it without hardware, and
the Arduino-dependent shim stays as thin as possible in `src/`.

### `EVSim/lib/net_state/` — pure connection policy, no Arduino dependency

A single decision function, with no I/O and no hidden clock:

```cpp
enum class NetPhase { Idle, Connecting, Connected, Backoff };
enum class NetAction { None, BeginConnect, Disconnect };

struct NetState {
  NetPhase phase;
  uint32_t phase_started_ms;
  uint32_t backoff_ms;   // delay before the next attempt; doubles per failure
};

struct NetStep {
  NetState state;    // the resulting state
  NetAction action;  // what the caller must do to reach it
};

// Pure: takes the current state by value and returns the next one. No clock is
// read inside -- `now_ms` is supplied, which is what makes the timeouts testable
// on the host without waiting real seconds.
NetStep net_step(NetState current, bool link_up, uint32_t now_ms);

// Stable short name for logging: "Idle", "Connecting", "Connected", "Backoff".
const char *net_phase_name(NetPhase phase);
```

This mirrors `pilot_request()` / `PilotResult` deliberately: state in by value, new state out,
no mutation and no hidden globals. The shim owns the one live `NetState` instance.

Policy it owns:

- **Association timeout: 10 s.** `Connecting` for longer than this is a failure.
- **Bounded exponential backoff: 1 s, 2 s, 4 s, 8 s, 16 s, capped at 30 s.** A downed AP
  produces one retry every 30 s, not a hot loop. The cap matters: unbounded backoff would
  eventually stop retrying for hours, and a dead-flat 1 s retry would hammer the radio and add
  avoidable CPU contention to the ISR.
- **Backoff resets to 1 s on a successful connection**, so a board that has been up for days
  and then sees a brief AP reboot reconnects in about a second, not thirty.
- **Time is a parameter, never read inside.** `now_ms` is passed in, which is what makes the
  timeouts testable without waiting for real seconds to pass.

`millis()` overflow is handled the way `main.cpp` already handles it: elapsed time is computed
as an unsigned difference, which is correct across the 49-day wraparound.

### `EVSim/src/net_wifi.{h,cpp}` — the Arduino shim

```c
void net_wifi_begin();              // non-blocking: starts the first attempt, returns
void net_wifi_poll();               // advances the state machine; never blocks
void net_wifi_print_status();       // '#'-prefixed status lines for the 'W' command
```

`net_wifi_begin()`, in this order, which matters:

1. `WiFi.persistent(false)` — stops the SDK writing credentials to NVS. Every such write is a
   flash operation that **disables the instruction cache on both cores**, which is the one
   mechanism by which WiFi can genuinely disturb the CP ISR. The credentials are compiled in,
   so persisting them buys nothing.
2. `WiFi.mode(WIFI_STA)` — station only; never start the soft AP.
3. `WiFi.config(kStaticIp, kGateway, kSubnetMask, kDns)` — **before** `WiFi.begin()`. Called
   after, the board associates and then waits out a DHCP lease that will never arrive, which
   presents as "connects, then has no IP" and is the single most likely way to get this wrong.
4. `WiFi.setSleep(false)` — modem sleep adds tens of milliseconds of latency to inbound
   packets, which would show up later as laggy pilot-state commands over MQTT.
5. `net_step()` returns `NetAction::BeginConnect`, which calls
   `WiFi.begin(kWifiSsid, kWifiPassword)`.

`net_wifi_poll()` maps `WiFi.status() == WL_CONNECTED` to `link_up`, calls `net_step()`,
executes the returned action, and prints a `#` line on any phase change. It performs no
waiting of any kind.

### `EVSim/src/main.cpp` — integration

- `net_wifi_begin()` is called **after** `cp_reader_begin()`, so CP measurement is live before
  the radio starts. Ordering mirrors the existing rationale for calling `pilot_driver_begin()`
  first.
- `net_wifi_poll()` is called at the top of `loop()`, beside `poll_serial()`. Because it never
  blocks, the 200 ms sample cadence is unaffected — the same property that makes
  `poll_serial()` safe today, where it handles at most one command per pass.
- **The CSV schema is unchanged.** Link status is *not* added as a column: `tools/charger_poll.py`
  merges on shared column names, and the calibration logs must stay comparable with those
  already recorded. Status goes to `#` lines only.
- A new **`W` command** prints phase, IP, gateway, mask, RSSI and channel. It is recognised in
  `main.cpp`'s `handle_command()` **before** the line reaches `pilot_parse_command()`, which is
  left unchanged. Reason: `lib/pilot_state` is the IEC 61851 state model, and a network status
  query is not a pilot state. Teaching its parser a letter that maps to no state would widen a
  pure, well-bounded unit for an unrelated caller's convenience.

## The ISR jitter question

`memory/evsim-firmware-layout.md` records that interrupt timing was chosen over the MCPWM
capture unit, to be revisited "only if WiFi introduces jitter". This design is where that gets
answered, so the reasoning is recorded rather than assumed.

**Why the risk is smaller than it looks.** `cp_isr()` is marked `IRAM_ATTR`, and
`attachInterrupt()` binds the handler to the core that calls it — `setup()`, which the Arduino
core runs on core 1. The ESP32's WiFi stack runs on core 0. Ordinary WiFi activity therefore
cannot preempt the CP ISR. Averaging helps too: a 200 ms window covers roughly 200 periods, so
isolated late edges are diluted rather than decisive.

**The two residual risks, and what is done about each.**

1. **Cache-disabling flash writes stall both cores** regardless of `IRAM_ATTR`. Addressed by
   `WiFi.persistent(false)`.
2. **`micros()` and `digitalRead()` are called from the ISR** and are not themselves guaranteed
   to be in IRAM. Left as-is: this is already true today and the reader is known to work. If
   step 1 does not suffice, replacing them with direct register reads is the next move.

**Verification is empirical, not analytical.** Success criteria are in *Testing*. If the
measurement does degrade, the escalation path, in order, is: a dedicated core-0 FreeRTOS task
for the network layer, then the MCPWM capture unit.

## Error handling

| Condition | Behaviour |
|---|---|
| AP absent or out of range | `Connecting` times out at 10 s, backoff, retry forever at up to 30 s intervals. CP measurement and CSV output continue throughout. |
| Wrong password | Indistinguishable from the above at this layer, so treated identically. `WiFi.status()` is reported by `W` so the cause is diagnosable by hand. |
| Static IP already in use | The board associates and reports `WL_CONNECTED`, but traffic fails. **Not detectable in this design** and explicitly out of scope — the assigned address is assumed unique. Symptom to recognise later: connects fine, IP correct, nothing answers. |
| AP reboots mid-session | `link_up` goes false, phase returns to `Backoff`, reconnects in about 1 s because the backoff was reset on the earlier success. |
| DNS absent or dead | No effect. Nothing resolves hostnames, by design. |
| Gateway wrong or absent | No effect on ESP32 -> AMTRON, which is same-subnet. Off-subnet hosts become unreachable. |

**The invariant, stated once:** no network condition may stop, delay or corrupt CP measurement.
The board is a bench instrument first and a network node second.

## Testing

**Host-side, `pio test -e native`** — covers `lib/net_state` only, since that is where the
logic is:

1. `Idle` yields `BeginConnect` and enters `Connecting`.
2. `link_up` while connecting yields `Connected` and `NetAction::None`.
3. No link for 10 s yields `Backoff`; no retry is issued before the backoff elapses.
4. Backoff doubles across consecutive failures and **caps at 30 s** — asserted at the cap, not
   merely below it.
5. A successful connection **resets the backoff to 1 s**, verified by then failing again and
   checking the delay is 1 s rather than the previous value.
6. Link lost while `Connected` moves to `Backoff` and reconnects.
7. Timeouts behave correctly across a `millis()` wraparound (`now_ms` near `UINT32_MAX`).

**On hardware** — a manual checklist, since none of it can be automated:

1. Flash, open the monitor, confirm a `#` line reporting the link up within a few seconds.
2. `W` reports IP `192.168.21.138`, mask `255.255.255.0`, gateway `192.168.21.130`.
3. `ping 192.168.21.138` from the Mac succeeds.
4. From the board's network, `tools/charger_poll.py` still reaches the AMTRON at
   `192.168.21.117` — confirms the two devices coexist on the subnet.
5. `A` -> `B` -> `C` still work and the B-before-C interlock still refuses `A` -> `C`.
6. **Jitter comparison, the point of the exercise:** log 60 s of CSV with the AP powered off,
   then 60 s connected, against a fixed charger current limit. Compare the standard deviation
   of `freq_hz` and `duty_pct`. Acceptance: `duty_pct` spread does not grow by more than
   0.1 percentage points — the resolution the reader claims — and `freq_hz` stays within 1 Hz
   of 1 kHz. Record the numbers in the memory notes either way; a null result is worth keeping.
7. Power-cycle the AP mid-log and confirm reconnection without a gap in the CSV rows.

## Follow-up work this unblocks

MQTT, in a separate design: outbound power setpoint, inbound pilot-state commands, on two
distinct topics ([[evsim-pilot-state-control]]). It inherits two constraints from this
document — **address the broker by IP** (DNS is not trustworthy), and **hold the last valid
setpoint** across the charger's expected no-load current oscillation
([[evsim-open-questions]]).

## Memory corrections this design requires

- `memory/evsim-amtron-rest-api.md`: charger address `192.168.188.186` -> `192.168.21.117`.
- `memory/evsim-open-questions.md`: drop the EEBus "Helios Home HEMS" entry and state the scope
  explicitly - the project is the loop AMTRON -> ESP32 -> Fronius and nothing else on the LAN.
- `memory/evsim-inverter-fronius.md`: record the inverter at `192.168.21.122`.
- A new note recording the network's addressing and the no-internet/no-DNS constraint.
