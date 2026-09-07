---
name: evsim-amtron-rest-api
description: Working recipe for the AMTRON REST API at 192.168.188.186 - Authorization token header, GET/POST /json/settings.json, and the OperatorCurrentLimit_vehicleif key that sets max current
metadata:
  type: reference
---

Charger: **http://192.168.188.186** (plain HTTP, HTTPS off). Controller is a **Phoenix Contact CC612**
(`ChargePointModel_branding` = `CC612_2S0R_CC`, vendor MENNEKES, serial `1349401.01393`) - so
Phoenix Contact CC612 / charge-control documentation applies to this unit.

## Authentication
A simple **`Authorization: <uuid>` header** - not a cookie, not a Bearer prefix. Obtain the token by
logging into the web UI and copying it from a request. Without it every call returns HTTP 401
`{"logged_in":false,"message":"Not logged in","code":1002}`.

Tokens are session-scoped and expire - if calls start 401-ing, grab a fresh one from the browser.

## Reading settings
`GET /json/settings.json` -> ~158 KB JSON: `{name, result:{reboot_required}, groups[], ChargePointInfo}`.
Each group has `sub_groups[].fields[]`, and every field carries `key`, `value`, `default`, `type`
(int/enum/string/triple), `mode` (**rw**/ro), `label`, `description`, and `options[]` for enums.
**267 settable fields.** Use `curl --compressed` - responses are gzip.

## Writing settings  <- THE CONTROL PATH FOR THIS THESIS
`POST /json/settings.json` with `Content-Type: application/json;charset=UTF-8`:

```
{"params":[{"key":"OperatorCurrentLimit_vehicleif","value":8}]}
```

Multiple keys can go in one `params` array.

## Key fields
- **`OperatorCurrentLimit_vehicleif`** (rw, int, A) - *the* max-current knob the web UI must drive.
- `InstallationCurrentLimit_vehicleif` = 16 A - the hard ceiling above it; the operator limit cannot exceed it.
- `TCPConfigOnOff_modbus_slave` (0=off), `TCPPortNumber_modbus_slave` = 502, `RegAddrSet_modbus_slave` = 2 -
  Modbus TCP server, if the fallback path is ever needed.
- `PowerLimit_energyman` = 22000 W, `PowerLimiterMode_energyman`.
- `InputCurrentLimit_energyman` = 16, `InputPowerLimit_energyman` (external-input energy management).

Related: [[evsim-amtron-live-telemetry]], [[evsim-amtron-interfaces]], [[evsim-tb-overview]].
