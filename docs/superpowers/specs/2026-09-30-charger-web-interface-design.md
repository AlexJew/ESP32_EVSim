# AMTRON Charger Web Interface — Design

**Date:** 2026-09-30  
**Status:** superseded: the page now uses Modbus TCP instead of REST; see [2026-10-01-charger-modbus-backend-design.md](2026-10-01-charger-modbus-backend-design.md). The REST session and credential sections below no longer apply.
**Scope:** a local web page for setting the AMTRON's operator current limit and reading the current values reported by the charger itself.

## Problem

The charger can already be controlled and queried through its REST interface, but the existing workflow requires command-line tools and a short-lived `Authorization` session ID copied from the charger web UI. A small local page should make the common read and set operations easier without requiring the user to copy that session ID manually.

The user is Alexandre, operating the EVSim bench from the development computer. The page needs to distinguish the configured maximum current from the charger's live signalled current and its metered current draw. Those values can differ, and metered current may be zero while the charger is temporarily offering no current.

## Goals

- Set the AMTRON `OperatorCurrentLimit_vehicleif` from a browser on the development computer.
- Read the configured operator limit and installation ceiling.
- Display both `SignaledCurrentLimit_vehicleif` and `OcppMeterCurrent_meter` as reported by the charger.
- Acquire and renew REST sessions automatically so the user does not copy an expiring session ID into the app.
- Keep charger credentials and session IDs out of the browser, logs, and version control.

## Non-goals

- Access from phones, tablets, or other devices on the lab WLAN.
- A general-purpose charger administration UI or editing unrelated settings.
- Pilot state control, inverter control, or changes to the MQTT setpoint server's averaging behavior.
- Defining the physical effect of a 0 A operator limit as pause, stop, or any other charger action; that behavior must be verified on this unit.
- Replacing the AMTRON's built-in web interface.

## User stories

- As the bench operator, I want to set the charger's maximum current in whole amps so I can choose the current used for a test without using `curl`.
- As the bench operator, I want to see the configured limit and installation ceiling so I can tell what I asked the charger to do and the upper bound it enforces.
- As the bench operator, I want to see the charger's signalled current and metered current draw separately so a zero draw is not confused with a zero offer or an API failure.
- As the bench operator, I want the app to report stale readings, failed writes, and session errors clearly so it does not present an old or unconfirmed value as current.

## Revised design

### Architecture

Implement the charger page as an independent Python package and process under `server/charger_web/`. Keep the charger REST client, HTTP API, static browser assets, and startup code inside that package. The page process talks to the AMTRON; the browser talks only to the local backend. It does not import or start the MQTT averaging service.

The service binds to `127.0.0.1` on the development computer (default port `8080`) and runs as a separate process, started with `python -m charger_web` from the `server` directory. The existing `python -m setpoint_server` command remains unchanged. The user starts and stops the two processes independently; process supervision through an OS service manager is outside this v1 scope.

Keep charger communication and session management in a dedicated client module. Keep HTTP routing and static page delivery in the same `charger_web` package. The existing MQTT client, input subscription, averaging window, and output topic remain unchanged and run in a separate process.

Serialize charger REST operations through one client lock. This prevents concurrent page requests from racing session renewal or interleaving a settings write with its readback. The page disables **Apply** while a write is in progress.

### Page layout and readings

The page contains one write control and two read-only measurements:

1. **Maximum current** — the configured `OperatorCurrentLimit_vehicleif`, editable as a whole number. Show the installation ceiling from `InstallationCurrentLimit_vehicleif` alongside it.
2. **Signalled current** — `SignaledCurrentLimit_vehicleif`, labelled as the limit the charger currently advertises to the simulated vehicle.
3. **Measured current draw** — `OcppMeterCurrent_meter`, shown per phase when the charger returns phase values. Display amperes and preserve the charger's individual phase readings.

The page refreshes live readings every two seconds while it is open. It also shows a last-successful-update time. A numeric zero is displayed as zero; it is not interpreted as missing data. Missing or malformed readings are shown as unavailable, not converted to zero.

The configured limit and installation ceiling are loaded when the page opens, refreshed after a write, and refreshed every 30 seconds while the page remains open. The live dashboard fields are refreshed every two seconds. Settings are not fetched on every live-reading poll.

### Setting the operator limit

Allowed values are **0 A or any whole amp value from 6 A through 16 A**, additionally capped at the charger-reported `InstallationCurrentLimit_vehicleif`. Values from 1–5 A and values above either ceiling are rejected by both the page and backend. The backend is authoritative; changing or bypassing browser validation must not allow an invalid write.

The operator enters a value and selects **Apply**. The backend:

1. Validates the value and confirms it is an integer in the allowed set.
2. Reads or refreshes the installation ceiling if needed and rejects a value above it.
3. Writes `OperatorCurrentLimit_vehicleif` through `POST /json/settings.json`.
4. Reads settings again and compares the charger-reported operator limit with the requested value.
5. Reports success only when readback matches. If it does not match, show the returned value and an error; never show an unconfirmed value as applied.

The UI labels a value of 0 as **0 A** only. It does not call it “pause” or “stop” until that effect is verified on the unit. Applying a new value is always explicit; loading the page or refreshing readings never writes to the charger.

### REST session and credentials

The backend acquires and renews the charger REST session automatically. The session ID is held in memory and is never sent to the browser, persisted to disk, or logged. On an authorization failure, the backend attempts one fresh login and retries the request once. If authentication still fails, it returns a clear error and does not loop indefinitely.

The username and password are supplied locally at runtime, for example through `EVSIM_CHARGER_USERNAME` and `EVSIM_CHARGER_PASSWORD` environment variables. They are not placed in source files, committed configuration, page HTML, or logs. The web page itself has no separate login screen in v1 because the service only binds to loopback.

The exact authentication exchange must be confirmed against this controller's firmware before implementation. Existing project notes document the expiring session ID used by `/json/settings.json`; current controller documentation describes a challenge/login flow, but the exact endpoints and compatibility with this unit have not yet been verified. Implement the client against the sequence observed on this CC612 rather than assuming endpoint names from another firmware generation.

The AMTRON currently serves plain HTTP. The local web service therefore remains bound to `127.0.0.1`; it must not be exposed on the lab WLAN in v1. Authentication values and session IDs are redacted from logs. A POST request must be same-origin.

### Backend API

The browser needs only two same-origin routes:

- `GET /api/charger/status` — returns configured limit, installation ceiling, signalled current, meter current by phase, per-reading freshness, and any current charger error. Live dashboard data is refreshed at most once per two seconds; operator settings are refreshed on page load, after a write, and every 30 seconds.
- `POST /api/charger/operator-limit` — accepts a JSON integer `current_limit_a`, validates it server-side, writes it, and returns the confirmed readback or a structured error.

The status response distinguishes `ok`, `stale`, `unavailable`, and `not_configured`. Each reading can be unavailable independently; a missing meter value does not hide a valid signalled-current value.

### Failure behavior

- **No credentials configured:** the page can load, but displays that charger access is not configured; reads and writes are unavailable. This must not stop the existing MQTT setpoint server.
- **Charger unreachable or request timeout:** return the last successful readings with their age and `stale` status. If no successful reading exists, return unavailable values.
- **Expired session:** attempt one re-login. If that fails, keep the last values marked stale and show an authentication error.
- **Rejected or invalid input:** return a validation error without making a charger write.
- **Write response missing, unsuccessful, or readback mismatch:** show a failed/unconfirmed result, refresh settings when possible, and display the actual readback. Do not silently repeat an ambiguous write.
- **A valid zero reading:** show `0 A` and the update time. It is not an error condition.

## Requirements and acceptance criteria

### Must have

- [ ] The page is available only at a loopback URL on the development computer and cannot be reached through the computer's lab-WLAN address.
- [ ] On page load, the backend reads the operator limit and installation limit from charger settings.
- [ ] Live dashboard readings refresh every two seconds while the page is open.
- [ ] The UI separately shows the signalled current and metered current draw, with units and per-phase meter values where available.
- [ ] The current-limit control accepts only 0 or whole amps from 6 through `min(16, installation_limit)`.
- [ ] Backend validation rejects booleans, other invalid types, fractions, 1–5 A, negative values, and values above the installation ceiling before a write.
- [ ] A valid write is sent to `OperatorCurrentLimit_vehicleif` and is reported as successful only after settings readback matches.
- [ ] A page load, refresh, or read-only poll never writes to the charger.
- [ ] A zero measured or signalled value remains distinguishable from missing data and from a stale/error state.
- [ ] Authentication is automatic; the browser never receives credentials or an AMTRON REST session ID.
- [ ] Credentials and tokens never appear in source control or application logs.
- [ ] Charger/network failures appear as stale or unavailable readings, with the last successful update time.
- [ ] Starting, stopping, or failing the charger web process does not stop or alter the separately running MQTT averaging process.

### Acceptance walkthrough

1. Start the MQTT setpoint server and the standalone charger web process in separate terminals. Open the local charger page URL.
2. Confirm the page reports the current `OperatorCurrentLimit_vehicleif`, installation limit, `SignaledCurrentLimit_vehicleif`, and `OcppMeterCurrent_meter` values from the charger.
3. Confirm live readings update about every two seconds and the settings values refresh after a write.
4. Apply a valid nonzero current limit such as 8 A. Confirm the backend reads back 8 A and the UI reports success. When the charger is offering current, confirm the signalled value follows as expected; do not require meter current to equal the limit.
5. Apply 0 A. Confirm the settings readback is 0 A. Separately record the charger's observed signaling and meter behavior; the UI must not assume 0 means pause or stop.
6. Try 1 A, 5 A, a fraction, a negative value, and a value above the installation ceiling. Confirm each is rejected without a charger write.
7. Disconnect the charger or invalidate the session. Confirm the page marks readings stale/unavailable, reports the error, and does not replace old values with zero or claim a write succeeded.
8. Stop the charger web process and confirm the separately running MQTT averaging process continues. Restart the page process, make the charger unreachable, and confirm MQTT averaging continues through the charger outage.

### Nice to have

- A manual refresh action for settings and live values.
- A small event line showing the last successful current-limit change and its timestamp.

## Success measures

- The operator can set a valid current value and see a matching charger readback without using `curl` or copying a session ID.
- The page distinguishes configured limit, signalled limit, and measured draw without representing stale/missing data as zero.
- A charger outage or session expiry does not interrupt MQTT averaging or falsely report a successful write.

## Open implementation checks

- **Blocking:** identify and exercise the exact login/session endpoint sequence used by the installed CC612 firmware, including the credential/role expected for reads and writes.
- **Blocking:** verify the charger accepts 0 for `OperatorCurrentLimit_vehicleif` and document the resulting CP signal and meter behavior. Acceptance of the setting value does not establish that it stops or pauses charging.
- **Non-blocking:** confirm the formatting of `OcppMeterCurrent_meter` on this unit and display all phase values without discarding a valid reading.
- **Non-blocking:** confirm whether the controller permits only one active REST session on this firmware and how the client recovers if the built-in charger UI invalidates its session.

## Dependencies and constraints

- AMTRON REST at `192.168.21.117`; development computer at `192.168.21.137` on the `hems` network.
- REST enabled; HTTPS is currently off.
- Existing working fields and routes are recorded in `memory/evsim-amtron-rest-api.md`, `memory/evsim-amtron-live-telemetry.md`, and `tools/charger_poll.py`.
- Existing setpoint server is Python and currently depends on `paho-mqtt`. Prefer the Python standard library for the small local HTTP service so this feature adds no web framework dependency.
- No tests or implementation are included in this design document.
