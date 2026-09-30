# AMTRON Charger Web Interface Implementation Plan

> **For agentic workers:** Implement inline from this plan and the design spec after the user approves this plan.

**Goal:** Add a loopback-only page to set the AMTRON operator current limit and view charger-reported current readings.

**Architecture:** Add an independent `charger_web` Python package and process under `server/charger_web/`, with a serialized REST client, standard-library HTTP server, and static browser assets. Start it with `python -m charger_web`; keep `setpoint_server` unchanged and run the two processes independently.

**Tech Stack:** Python standard library (`urllib`, `http.server`, `threading`), HTML, CSS, and browser JavaScript. The charger web package has no dependency on `paho-mqtt`.

**Spec:** `docs/superpowers/specs/2026-09-30-charger-web-interface-design.md`

## Global Constraints

- Bind the page service to `127.0.0.1` only.
- Allowed limit values are 0 A or whole amps from 6 A through `min(16, installation_limit)`.
- Keep credentials and session IDs out of browser responses, source control, and logs; hold the session ID in memory.
- Confirm a write only after reading back the exact operator limit.
- Charger web startup, shutdown, or failures must not stop or affect the separate MQTT averaging process.
- Do not add a web framework dependency.
- The CC612 firmware's exact login exchange remains to be confirmed on the unit before claiming end-to-end operation.
- Do not add or run tests for this task; inspect changes and perform non-test syntax checks only.
- Do not send charger credentials or session IDs through chat or write them to project files.

## Review Focus

- Expired or invalid session: retry login once, then report an error and keep last good values stale.
- Invalid request JSON, booleans, fractions, and limits outside the accepted set: reject before contacting the write endpoint.
- Missing, malformed, or phase-structured meter values: preserve valid independent readings and do not substitute zero.
- Slow or unreachable charger, web-process crash, or web-process shutdown: the independent MQTT process must continue unchanged.
- Cross-origin writes and non-loopback access: reject requests outside the same-origin, loopback interface.

---

### Task 1: Standalone charger web package and REST client

**Files:**
- Create: `server/charger_web/__init__.py`
- Create: `server/charger_web/__main__.py`
- Create: `server/charger_web/config.py`
- Create: `server/charger_web/charger.py`

**Interface produced:**

- `ChargerClient.status() -> dict` returns overall status and the four readings `operator_limit_a`, `installation_limit_a`, `signaled_current_a`, and `meter_current_a`. Each reading is `{value, status, updated_at}`; `status` is `ok`, `stale`, or `unavailable`. Top-level status is `ok`, `stale`, `unavailable`, or `not_configured`.
- `ChargerClient.set_operator_limit(value: int) -> dict` validates, writes, reads settings back, and returns `{success: True, operator_limit_a, updated_at}` only if the exact value is confirmed. It raises `ChargerError` with a safe display message otherwise.

**Steps:**

- [ ] Create a standalone package that imports no `setpoint_server` modules and no MQTT dependencies. Read username/password from `EVSIM_CHARGER_USERNAME` and `EVSIM_CHARGER_PASSWORD`; default charger host to `192.168.21.117` and allow `EVSIM_CHARGER_HOST` to override it.
- [ ] Add a `python -m charger_web` entry point with a loopback-only `--host` (default `127.0.0.1`) and optional `--port` (default 8080). Reject a host other than `127.0.0.1`.
- [ ] Implement a standard-library HTTP request helper with a 3-second timeout, gzip decoding, JSON parsing, `Authorization: <session-id>`, and error messages that never include request headers or response bodies.
- [ ] Implement challenge login using the CC612 flow verified on the device. The Bender docs describe `GET /v1/login`, SHA-256 of password concatenated with the challenge token, then `POST /v1/login`; the lab controller returned HTTP 200 to the GET probe. Check the actual login response/session schema locally before treating authentication as verified.
- [ ] Keep the returned session ID in memory. On a 401, log in once and retry that operation once; return a clear failure if renewal also fails.
- [ ] Serialize login, reads, writes, and readback through one lock. Parse settings and dashboard fields from the existing `/json/settings.json` and `/json/dashboard.json` formats.
- [ ] Cache settings for 30 seconds and dashboard readings for two seconds. Keep the last good data and mark it stale when a refresh fails. Track reading timestamps separately so a missing meter value does not invalidate a valid signal reading.
- [ ] Normalize scalar currents and meter phase values while preserving numeric zero. Represent malformed or absent measurements as unavailable, never as zero.
- [ ] Validate that `value` has exact type `int` (so booleans are rejected), is 0 or 6–16, and is no greater than the charger installation limit. Refresh settings before a write, POST only `OperatorCurrentLimit_vehicleif`, force settings readback, and return success only on exact match.

**Review:** Confirm no session ID, password, or challenge token reaches logs, disk, or the browser response. Confirm startup without credentials returns `not_configured` and does not raise out of the client.

### Task 2: Local HTTP API and page

**Files:**
- Create: `server/charger_web/web.py`
- Create: `server/charger_web/static/index.html`
- Create: `server/charger_web/static/app.css`
- Create: `server/charger_web/static/app.js`

**Interface consumed:** `ChargerClient.status()` and `ChargerClient.set_operator_limit(value)` from Task 1.

**Steps:**

- [ ] Implement the HTTP service within `charger_web`; serve only named package assets, and keep request handling in the independent charger web process.
- [ ] Serve `/` and only the named JavaScript and CSS assets. Do not allow arbitrary filesystem paths.
- [ ] Implement `GET /api/charger/status` returning `ChargerClient.status()` as JSON.
- [ ] Implement `POST /api/charger/operator-limit`; require same-origin `Origin`, exact expected `Host`, JSON content type, a body no larger than 1024 bytes, and exactly one integer property named `current_limit_a`. Return structured validation or charger errors without echoing credentials or tokens.
- [ ] Render configured operator limit, installation ceiling, signalled current, per-phase meter current, reading freshness, and update times. Display numeric zero as `0 A`.
- [ ] Poll status every two seconds; rely on the client cache for settings refresh every 30 seconds. Keep settings separate from dashboard refresh cadence.
- [ ] Disable Apply while a write is pending; display success only when the API returns the matching charger readback. Require an explicit button action to write.
- [ ] Keep the page usable in unavailable and not-configured states, and make stale values visibly distinct from fresh readings.

**Review:** Confirm requests from another LAN address cannot reach the listener and cross-origin POST requests are rejected.

### Task 3: Operator documentation

**Files:**
- Modify: `server/README.md`

**Steps:**

- [ ] Document the independent startup command from `server/`, credential environment variables, optional host/port settings, local URL, CC612 login firmware dependency, and loopback-only restriction.
- [ ] Document that the setpoint server and charger web server start and stop as separate processes. Do not change the setpoint server entry point or its MQTT behavior.
- [ ] Show a two-terminal development workflow and explain that stopping either process leaves the other running.

**Review:** Confirm the documented charger page has no code path that starts, stops, imports, or signals the MQTT averaging process.

### Task 4: Review and verification

Inspect the final diff against the design spec, check for accidental edits to the unrelated inverter files, and run `python -m compileall -q server/charger_web` as a syntax check (not a test). Do not claim hardware authentication/write acceptance until the login flow and 0 A behavior are verified on the charger. Do not issue a charger setting write as part of implementation verification.
