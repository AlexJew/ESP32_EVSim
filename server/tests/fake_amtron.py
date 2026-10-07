"""A fake AMTRON Modbus TCP server on localhost, for the charger page's tests.

Holding registers only (function codes 3 and 6; function code 4 is refused with exception 2, as on the
real unit). It records every request, and can imitate the charger's fallback: after fall_back() the
next request sees the safe current (limit 0, signalled 0), and the charger then restores the stored limit.
Nothing here touches the real charger.
"""

import socket
import socketserver
import struct
import threading

DEFAULTS = {
    1000: 10,                 # HEMS_CURRENT_LIMIT
    134: 16,                  # OPERATOR_CURRENT_LIMIT
    133: 16,                  # installation limit (inferred)
    706: 10,                  # SIGNALED_CURRENT
    122: 3,                   # VEHICLE_STATE: C
    104: 1,                   # OCPP_CP_STATUS: Occupied
    212: 0, 213: 874,         # METER_CUR_L1 = 874 mA (high word, low word)
    214: 0, 215: 651,
    216: 0, 217: 808,
    220: 0, 221: 600,         # total active power, 600 W
    222: 0, 223: 231,         # phase voltages L1, L2, L3, V
    224: 0, 225: 232,
    226: 0, 227: 233,
}


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        fake = self.server.fake
        fake.clients.append(self.request)
        while True:
            head = self.request.recv(7)
            if len(head) < 7:
                return
            tid, _, length, unit = struct.unpack(">HHHB", head)
            body = self.request.recv(length - 1)
            fc = body[0]
            addr, arg = struct.unpack(">HH", body[1:5])
            with fake.lock:
                out = fake.respond(fc, addr, arg, body)
            self.request.sendall(struct.pack(">HHHB", tid, 0, len(out) + 1, unit) + out)


class FakeAmtron:
    def __init__(self):
        self.regs = dict(DEFAULTS)
        self.log = []                 # (function code, address, count or value)
        self.writes = []              # (address, value)
        self.refuse_writes = False    # answer every write with Modbus exception 3
        self.refuse_reads = set()     # addresses answered with exception 2
        self.ignore_writes = False    # echo a write but keep the old value
        self.ignore_signal_updates = False   # 706 does not follow a write at once (the real unit takes ~3 s)
        self.in_fallback = False
        self._stored = None
        self.clients = []
        self.lock = threading.Lock()
        self._server = None

    def start(self):
        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.fake = self
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
        return self._server.server_address[1]

    def stop(self):
        for client in list(self.clients):
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self._server.shutdown()
        self._server.server_close()

    def set_u32(self, address, value):
        self.regs[address], self.regs[address + 1] = value >> 16, value & 0xFFFF

    def fall_back(self):
        """The charger lost its Modbus master: it signals the safe current (0 A) until the next request."""
        self._stored = self.regs[1000]
        self.regs[1000] = 0
        self.regs[706] = 0
        self.in_fallback = True

    def _signal(self):
        self.regs[706] = min(self.regs[1000], self.regs[134])

    def _restore(self):
        self.regs[1000] = self._stored
        self._signal()
        self.in_fallback = False

    def respond(self, fc, addr, arg, body):
        self.log.append((fc, addr, arg))
        restore_after = self.in_fallback
        try:
            if fc == 3:
                addrs = range(addr, addr + arg)
                if any(a not in self.regs or a in self.refuse_reads for a in addrs):
                    return bytes([fc | 0x80, 2])
                return bytes([fc, 2 * arg]) + struct.pack(">%dH" % arg, *(self.regs[a] for a in addrs))
            if fc == 6:
                self.writes.append((addr, arg))
                if self.refuse_writes or addr not in self.regs:
                    return bytes([fc | 0x80, 3])
                if not self.ignore_writes:
                    self.regs[addr] = arg
                    if addr == 1000 and not self.ignore_signal_updates:
                        self._signal()          # the signalled current follows the limit
                return body[:5]
            return bytes([fc | 0x80, 2])        # input registers and everything else: refused
        finally:
            if restore_after:
                self._restore()                 # the request that found it in fallback sees 0; later ones see the limit
