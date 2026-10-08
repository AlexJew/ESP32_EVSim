"""The recorder's SQLite file and the thread that writes it.

Everything the recorder sees is queued and written in batches by one thread, so a slow disk never delays an MQTT
callback or a charger poll. The queue is bounded: when it is full the oldest rows are dropped and counted. The file is in
WAL mode, so it can be read while a run is in progress. Rows are only ever added; the views (cp, setpoint, inverter)
read the messages table with SQLite's JSON functions and give NULL for anything they cannot read.
"""

import json
import logging
import sqlite3
import threading
import time
from collections import deque

log = logging.getLogger("evsim_recorder.storage")

QUEUE_MAX = 100_000
BATCH_ROWS = 200
BATCH_SECONDS = 0.5
BATCH_MAX = 2000                  # rows in one transaction when the queue is long

CHARGER_VALUES = (
    "set_limit_a", "operator_limit_a", "installation_limit_a", "signalled_current_a", "vehicle_state", "charger_status",
    "i_l1_a", "i_l2_a", "i_l3_a", "v_l1_v", "v_l2_v", "v_l3_v", "power_w",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY, started_ns INTEGER NOT NULL, ended_ns INTEGER,
  broker TEXT, charger TEXT, plan TEXT, argv TEXT, version TEXT);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, t_ns INTEGER NOT NULL, mono_ns INTEGER NOT NULL,
  topic TEXT NOT NULL, retained INTEGER NOT NULL, payload TEXT, payload_raw BLOB);
CREATE INDEX IF NOT EXISTS messages_topic_time ON messages (topic, t_ns);
CREATE TABLE IF NOT EXISTS charger (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, t_start_ns INTEGER NOT NULL, t_end_ns INTEGER NOT NULL,
  set_limit_a INTEGER, operator_limit_a INTEGER, installation_limit_a INTEGER, signalled_current_a INTEGER,
  vehicle_state INTEGER, charger_status INTEGER,
  i_l1_a REAL, i_l2_a REAL, i_l3_a REAL, v_l1_v REAL, v_l2_v REAL, v_l3_v REAL, power_w REAL, error TEXT);
CREATE TABLE IF NOT EXISTS limit_writes (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, run INTEGER, step INTEGER, t_write_ns INTEGER, t_ack_ns INTEGER,
  from_a INTEGER, to_a INTEGER, readback_a INTEGER, label TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS recorder_events (
  id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, t_ns INTEGER NOT NULL, kind TEXT NOT NULL, detail TEXT);
"""


def _view(name, topic, columns):
    """A view over messages for one topic: the typed JSON fields, NULL for a payload that is not a JSON object."""
    fields = ",\n  ".join("json_extract(j, '$.%s') AS %s" % (key, alias) for alias, key in columns)
    return f"""
CREATE VIEW IF NOT EXISTS {name} AS
SELECT session_id, t_ns, mono_ns,
  {fields}
FROM (SELECT session_id, t_ns, mono_ns,
        CASE WHEN json_valid(payload) THEN CASE WHEN json_type(payload) = 'object' THEN payload END END AS j
      FROM messages WHERE topic = '{topic}' AND retained = 0);
"""


VIEWS = (
    _view("cp", "evsim/cp", (
        ("board_ms", "t"), ("freq", "freq"), ("duty", "duty"), ("adc", "adc"), ("cp", "cp"), ("state", "state"),
        ("amps", "amps"), ("watts", "watts"), ("setpoint_w", "setpoint_w"), ("status", "status"), ("age_ms", "age_ms"))),
    _view("setpoint", "evsim/setpoint", (
        ("setpoint_w", "setpoint_w"), ("samples", "samples"), ("window_s", "window_s"), ("status", "status"),
        ("server_ts", "ts"), ("tick_ms", "ts_ms"))),
    _view("inverter", "evsim/inverter", (
        ("mode", "mode"), ("target_dc_w", "target_dc_w"), ("cap_ac_w", "cap_ac_w"), ("dc_w", "dc_w"), ("dc_a", "dc_a"),
        ("dc_v", "dc_v"), ("ac_w", "ac_w"), ("ac_a", "ac_a"), ("ac_v", "ac_v"), ("inverter_state", "inverter_state"),
        ("error", "error"), ("server_ts", "ts"), ("read_ts_ms", "read_ts_ms"), ("cap_ts_ms", "cap_ts_ms"))),
)

INSERTS = {
    "messages": "INSERT INTO messages (session_id, t_ns, mono_ns, topic, retained, payload, payload_raw) VALUES (?,?,?,?,?,?,?)",
    "charger": "INSERT INTO charger (session_id, t_start_ns, t_end_ns, %s, error) VALUES (?,?,?,%s,?)"
               % (", ".join(CHARGER_VALUES), ",".join("?" * len(CHARGER_VALUES))),
    "limit_writes": "INSERT INTO limit_writes (session_id, run, step, t_write_ns, t_ack_ns, from_a, to_a, readback_a, label, error) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
    "recorder_events": "INSERT INTO recorder_events (session_id, t_ns, kind, detail) VALUES (?,?,?,?)",
}


class Store:
    def __init__(self, path, queue_max=QUEUE_MAX, batch_rows=BATCH_ROWS, batch_seconds=BATCH_SECONDS, retry_seconds=1.0):
        # autocommit: the writer opens and commits its own transactions. check_same_thread is off because the
        # connection is made here but used by the writer thread; nothing else touches it while that thread runs.
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA + "".join(VIEWS))
        self._queue_max, self._batch_rows, self._batch_seconds, self._retry_seconds = queue_max, batch_rows, batch_seconds, retry_seconds
        self._rows = deque()
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._idle = threading.Condition(self._lock)
        self._busy = False
        self._stopping = False
        self._dropped = 0
        self._notes = []                     # write errors waiting to be recorded as events
        self._thread = None
        self._closed = False
        self.session_id = None

    # -- the session ---------------------------------------------------------------------------

    def start_session(self, broker, charger, plan, argv):
        from . import __version__
        cursor = self._db.execute(
            "INSERT INTO sessions (started_ns, broker, charger, plan, argv, version) VALUES (?,?,?,?,?,?)",
            (time.time_ns(), broker, charger, json.dumps(plan), json.dumps(list(argv)), __version__))
        self.session_id = cursor.lastrowid
        return self.session_id

    # -- adding rows: these only enqueue ------------------------------------------------------------

    def put(self, table, row):
        with self._wake:
            if len(self._rows) >= self._queue_max:
                self._rows.popleft()
                self._dropped += 1
            self._rows.append((table, row))
            self._wake.notify()

    def add_message(self, t_ns, mono_ns, topic, retained, payload):
        payload = bytes(payload)
        try:
            text, raw = payload.decode("utf-8"), None
        except UnicodeDecodeError:
            text, raw = payload.decode("utf-8", "replace"), payload
        self.put("messages", (self.session_id, t_ns, mono_ns, topic, int(bool(retained)), text, raw))

    def add_charger(self, t_start_ns, t_end_ns, error=None, **values):
        self.put("charger", (self.session_id, t_start_ns, t_end_ns, *(values.get(name) for name in CHARGER_VALUES), error))

    def add_limit_write(self, run, step, t_write_ns, t_ack_ns, from_a, to_a, readback_a, label, error=None):
        self.put("limit_writes", (self.session_id, run, step, t_write_ns, t_ack_ns, from_a, to_a, readback_a, label, error))

    def add_event(self, kind, detail="", t_ns=None):
        self.put("recorder_events", (self.session_id, time.time_ns() if t_ns is None else t_ns, kind, detail))

    # -- the writer thread -----------------------------------------------------------------------------

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="recorder-writer", daemon=True)
            self._thread.start()

    def _take(self):
        """Wait for a batch: up to batch_rows, or whatever has arrived after batch_seconds. None when stopped and empty."""
        with self._wake:
            while not self._rows and not self._stopping:
                self._wake.wait()
            if not self._rows:
                return None
            deadline = time.monotonic() + self._batch_seconds
            while len(self._rows) < self._batch_rows and not self._stopping:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._wake.wait(remaining)
            batch = [self._rows.popleft() for _ in range(min(len(self._rows), BATCH_MAX))]
            dropped, self._dropped = self._dropped, 0
            self._busy = True
        if dropped:
            batch.append(("recorder_events", (self.session_id, time.time_ns(), "dropped",
                                              f"{dropped} rows dropped: the write queue was full")))
        return batch

    def _transaction(self, rows):
        self._db.execute("BEGIN")
        try:
            for table, row in rows:
                self._db.execute(INSERTS[table], row)
        except Exception:
            self._db.execute("ROLLBACK")
            raise
        self._db.execute("COMMIT")

    def _write(self, batch):
        while True:
            try:
                self._transaction(batch)
                break
            except sqlite3.Error as exc:
                log.error("cannot write %d rows (%s); retrying", len(batch), exc)
                self._notes.append(str(exc))
                if self._stopping:
                    return                       # shutting down: one more try was made, give up rather than hang
                time.sleep(self._retry_seconds)
        if self._notes:
            notes, self._notes = self._notes, []
            events = [("recorder_events", (self.session_id, time.time_ns(), "write_error", note)) for note in notes]
            try:
                self._transaction(events)
            except sqlite3.Error:
                log.exception("cannot record the write errors")

    def _run(self):
        while True:
            batch = self._take()
            if batch is None:
                return
            try:
                self._write(batch)
            except Exception:                    # the writer must survive anything
                log.exception("the recorder's writer failed on a batch")
            finally:
                with self._idle:
                    self._busy = False
                    self._idle.notify_all()

    def flush(self, timeout=5.0):
        """Wait until everything queued so far has been committed (or dropped). False on timeout."""
        deadline = time.monotonic() + timeout
        with self._idle:
            while self._rows or self._busy:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._wake.notify()
                self._idle.wait(min(remaining, 0.05))
        return True

    def stop(self):
        """Drain the queue, mark the session ended and close the file. Safe to call twice."""
        if self._closed:
            return
        with self._wake:
            self._stopping = True
            self._wake.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=30)
        self._closed = True
        try:
            if self.session_id is not None:
                self._db.execute("UPDATE sessions SET ended_ns = ? WHERE id = ?", (time.time_ns(), self.session_id))
        except sqlite3.Error:
            log.exception("cannot mark the session ended")
        finally:
            self._db.close()
