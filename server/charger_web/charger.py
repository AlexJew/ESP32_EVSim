"""REST client for the AMTRON charge controller.

This process-local client obtains the CC612 session automatically, serializes
charger I/O, and keeps the last successful measurements for stale display.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import math
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from . import config

log = logging.getLogger("charger_web.charger")

OPERATOR_KEY = "OperatorCurrentLimit_vehicleif"
INSTALLATION_KEY = "InstallationCurrentLimit_vehicleif"
SIGNALLED_KEY = "SignaledCurrentLimit_vehicleif"
METER_KEY = "OcppMeterCurrent_meter"


class ChargerError(RuntimeError):
    """A safe-to-display charger or communication error."""


class LimitValidationError(ChargerError):
    """The requested current limit is outside the accepted range."""


class _Unauthorized(Exception):
    """Internal signal used to renew an expired REST session."""


def _as_items(value):
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []


def _fields(document):
    """Yield setting/dashboard fields across list- or object-shaped groups."""
    if not isinstance(document, dict):
        return
    result = document.get("result")
    groups = document.get("groups")
    if groups is None and isinstance(result, dict):
        groups = result.get("groups")
    for group in _as_items(groups):
        if not isinstance(group, dict):
            continue
        yield from (field for field in _as_items(group.get("fields")) if isinstance(field, dict))
        for subgroup in _as_items(group.get("sub_groups")):
            if isinstance(subgroup, dict):
                yield from (field for field in _as_items(subgroup.get("fields")) if isinstance(field, dict))


def _field_map(document):
    return {field["key"]: field.get("value") for field in _fields(document) if field.get("key")}


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    token = str(value).strip().split(maxsplit=1)[0] if str(value).strip() else ""
    try:
        number = float(token)
        return number if math.isfinite(number) else None
    except ValueError:
        return None


def _integer(value):
    number = _number(value)
    if number is None or not math.isfinite(number) or not number.is_integer():
        return None
    return int(number)


def _meter_value(value):
    """Return a scalar meter value or a phase-name-to-amps map."""
    if isinstance(value, dict):
        phases = {str(phase): _number(reading) for phase, reading in value.items()}
        phases = {phase: reading for phase, reading in phases.items() if reading is not None}
        return phases or None
    if isinstance(value, (list, tuple)):
        phases = {f"L{index}": _number(reading) for index, reading in enumerate(value, 1)}
        phases = {phase: reading for phase, reading in phases.items() if reading is not None}
        return phases or None
    return _number(value)


class ChargerClient:
    """Serialized REST access with in-memory auth and separate data caches."""

    def __init__(self, host=None, username=None, password=None, clock=time.monotonic):
        self.host = host or config.charger_host()
        default_username, default_password = config.charger_credentials()
        self.username = default_username if username is None else username
        self.password = default_password if password is None else password
        self._clock = clock
        self._lock = threading.RLock()
        self._session_id = None
        self._login_retry_at = 0.0
        self._settings = None
        self._settings_at = None
        self._settings_attempt_at = None
        self._settings_error = None
        self._dashboard_attempt_at = None
        self._dashboard_error = None
        self._dashboard_values = {SIGNALLED_KEY: None, METER_KEY: None}
        self._dashboard_reading_at = {SIGNALLED_KEY: None, METER_KEY: None}

    @property
    def configured(self):
        return bool(self.username and self.password)

    def _url(self, path):
        return f"http://{self.host}{path}"

    @staticmethod
    def _decode(raw):
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _open(self, path, method="GET", payload=None, authorized=True):
        headers = {"Accept": "application/json", "Accept-Encoding": "gzip"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json;charset=UTF-8"
        if authorized:
            if not self._session_id:
                self._login()
            headers["Authorization"] = self._session_id
        request = urllib.request.Request(self._url(path), data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=config.HTTP_TIMEOUT_S) as response:
                return self._decode(response.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 401 and authorized:
                self._session_id = None
                raise _Unauthorized from exc
            raise ChargerError(f"Charger returned HTTP {exc.code}.") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ChargerError("Could not reach the charger.") from exc
        except (UnicodeDecodeError, json.JSONDecodeError, gzip.BadGzipFile) as exc:
            raise ChargerError("Charger returned an unreadable response.") from exc

    def _login(self):
        if not self.configured:
            raise ChargerError("Set EVSIM_CHARGER_USERNAME and EVSIM_CHARGER_PASSWORD locally to enable charger access.")
        if self._clock() < self._login_retry_at:
            raise ChargerError("A recent charger login failed; the client will retry shortly.")
        try:
            challenge = self._open("/v1/login", authorized=False)
            token = challenge.get("token") if isinstance(challenge, dict) else None
            if not isinstance(token, str) or not token:
                raise ChargerError("Charger login did not return a login challenge.")
            # CC612 challenge flow: SHA-256(password + one-use challenge token).
            proof = hashlib.sha256((self.password + token).encode("utf-8")).hexdigest()
            result = self._open(
                "/v1/login",
                method="POST",
                payload={"username": self.username, "password": proof},
                authorized=False,
            )
            session = result.get("session", {}) if isinstance(result, dict) else {}
            session_id = session.get("id") if isinstance(session, dict) else None
            if not isinstance(result, dict) or not result.get("logged_in") or not isinstance(session_id, str) or not session_id:
                raise ChargerError("Charger rejected the configured username or password.")
        except ChargerError:
            self._login_retry_at = self._clock() + config.LOGIN_RETRY_DELAY_S
            raise
        self._session_id = session_id
        self._login_retry_at = 0.0

    def _request(self, path, method="GET", payload=None, retried=False):
        try:
            return self._open(path, method=method, payload=payload)
        except _Unauthorized:
            if retried:
                raise ChargerError("Charger authorization failed after one new login.")
            self._login()
            return self._request(path, method=method, payload=payload, retried=True)

    def _refresh_settings(self, force=False):
        now = self._clock()
        if not force and self._settings is not None and now - self._settings_at < config.SETTINGS_CACHE_S:
            return
        if not force and self._settings_attempt_at is not None and now - self._settings_attempt_at < config.SETTINGS_CACHE_S:
            return
        self._settings_attempt_at = now
        values = _field_map(self._request("/json/settings.json"))
        if OPERATOR_KEY not in values or INSTALLATION_KEY not in values:
            raise ChargerError("Charger settings did not contain the current-limit fields.")
        self._settings = values
        self._settings_at = self._clock()
        self._settings_error = None

    def _refresh_dashboard(self, force=False):
        now = self._clock()
        if not force and self._dashboard_attempt_at is not None and now - self._dashboard_attempt_at < config.DASHBOARD_CACHE_S:
            return
        self._dashboard_attempt_at = now
        values = _field_map(self._request("/json/dashboard.json"))
        sampled_at = self._clock()
        normalizers = {SIGNALLED_KEY: _number, METER_KEY: _meter_value}
        for key, normalize in normalizers.items():
            self._dashboard_values[key] = normalize(values.get(key)) if key in values else None
            self._dashboard_reading_at[key] = sampled_at
        self._dashboard_error = None

    @staticmethod
    def _timestamp(monotonic_at):
        if monotonic_at is None:
            return None
        age = max(0.0, time.monotonic() - monotonic_at)
        return datetime.fromtimestamp(time.time() - age, timezone.utc).isoformat()

    def _reading(self, value, sampled_at, stale_after, failed=False):
        status = "unavailable" if value is None or sampled_at is None else "ok"
        if status == "ok" and (failed or self._clock() - sampled_at >= stale_after):
            status = "stale"
        return {"value": value, "status": status, "updated_at": self._timestamp(sampled_at)}

    def status(self):
        with self._lock:
            if not self.configured:
                unavailable = self._reading(None, None)
                return {
                    "status": "not_configured",
                    "error": "Charger credentials are not configured.",
                    "operator_limit_a": dict(unavailable),
                    "installation_limit_a": dict(unavailable),
                    "signaled_current_a": dict(unavailable),
                    "meter_current_a": dict(unavailable),
                }

            errors = []
            try:
                self._refresh_settings()
            except ChargerError as exc:
                self._settings_error = str(exc)
            try:
                self._refresh_dashboard()
            except ChargerError as exc:
                self._dashboard_error = str(exc)
            settings_failed = self._settings_error is not None
            dashboard_failed = self._dashboard_error is not None
            if self._settings_error:
                errors.append(self._settings_error)
            if self._dashboard_error:
                errors.append(self._dashboard_error)

            settings = self._settings or {}
            readings = {
                "operator_limit_a": self._reading(_integer(settings.get(OPERATOR_KEY)), self._settings_at, config.SETTINGS_CACHE_S * 1.5, settings_failed),
                "installation_limit_a": self._reading(_integer(settings.get(INSTALLATION_KEY)), self._settings_at, config.SETTINGS_CACHE_S * 1.5, settings_failed),
                "signaled_current_a": self._reading(self._dashboard_values[SIGNALLED_KEY], self._dashboard_reading_at[SIGNALLED_KEY], config.DASHBOARD_CACHE_S * 1.5, dashboard_failed),
                "meter_current_a": self._reading(self._dashboard_values[METER_KEY], self._dashboard_reading_at[METER_KEY], config.DASHBOARD_CACHE_S * 1.5, dashboard_failed),
            }
            if errors:
                overall = "stale" if any(reading["value"] is not None for reading in readings.values()) else "unavailable"
            elif any(reading["status"] == "stale" for reading in readings.values()):
                overall = "stale"
            else:
                overall = "ok"
            return {"status": overall, "error": errors[-1] if errors else None, **readings}

    @staticmethod
    def _validate_limit(value, installation_limit):
        if type(value) is not int:
            raise LimitValidationError("Current limit must be a whole number of amps.")
        if value != 0 and not 6 <= value <= 16:
            raise LimitValidationError("Current limit must be 0 A or a whole value from 6 A to 16 A.")
        if installation_limit is None:
            raise ChargerError("Installation current limit is unavailable; no write was made.")
        if value > installation_limit:
            raise LimitValidationError(f"Current limit cannot exceed the installation limit of {installation_limit} A.")

    def set_operator_limit(self, value):
        with self._lock:
            if not self.configured:
                raise ChargerError("Charger credentials are not configured; no write was made.")
            self._refresh_settings(force=True)
            installation_limit = _integer((self._settings or {}).get(INSTALLATION_KEY))
            self._validate_limit(value, installation_limit)
            # Do not blindly retry a POST after an ambiguous network failure.
            try:
                self._request(
                    "/json/settings.json",
                    method="POST",
                    payload={"params": [{"key": OPERATOR_KEY, "value": value}]},
                )
            except ChargerError as write_error:
                # The request may have reached the controller before the
                # connection failed. Refresh once to show its actual value, but
                # never turn an ambiguous response into reported write success.
                try:
                    self._refresh_settings(force=True)
                except ChargerError as readback_error:
                    self._settings_error = str(readback_error)
                raise ChargerError(
                    "The charger did not confirm the write response; check the refreshed setting before retrying."
                ) from write_error
            self._refresh_settings(force=True)
            confirmed = _integer((self._settings or {}).get(OPERATOR_KEY))
            if confirmed != value:
                raise ChargerError(f"Write was not confirmed; charger reports {confirmed!r} A.")
            return {"success": True, "operator_limit_a": confirmed, "updated_at": self._timestamp(self._settings_at)}
