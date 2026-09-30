"""A fake GEN24 that speaks Modbus TCP (function codes 3 and 6) on localhost.

The SunSpec layout is copied from the real GEN24 12.0 Plus SC read on 2026-09-30. It records every
write. Used by the client, writer and bench tests; nothing here touches the real inverter.
"""

import socket
import socketserver
import struct
import threading

# Wire addresses of the real unit's model chain (first data register of each model).
M1, M103, M120, M121, M123, M160 = 40004, 40071, 40123, 40151, 40229, 40255
END = 40369


def text_regs(s, n):
    b = s.encode().ljust(2 * n, b"\0") if isinstance(s, str) else s
    return list(struct.unpack(">%dH" % n, b))


class FakeInverter:
    def __init__(self, with_123=True):
        self.regs = {a: 0 for a in range(40000, END + 2)}
        self.writes = []            # (address, value), in order
        self.refuse_writes = False  # answer every write with Modbus exception 3
        self.refuse = set()         # addresses whose writes get exception 3
        self.clients = []           # open connections, closed by stop()
        self._chain(with_123)
        self._values()
        self._server = None

    def put(self, addr, values):
        for i, v in enumerate(values):
            self.regs[addr + i] = v & 0xFFFF

    def _chain(self, with_123):
        self.put(40000, [0x5375, 0x6E53])
        chain = [(1, 65), (103, 50), (120, 26), (121, 30), (122, 44)]
        if with_123:
            chain.append((123, 24))
        chain += [(160, 88), (124, 24)]
        addr = 40002
        for mid, length in chain:
            self.put(addr, [mid, length])
            addr += 2 + length
        self.put(addr, [0xFFFF, 0])
        assert addr == END or not with_123

    def _values(self):
        self.put(M1, text_regs("Fronius", 16) + text_regs("Symo GEN24 12.0 Plus SC", 16))
        self.put(M1 + 40, text_regs("1.41.11-1", 8))
        self.put(M103 + 12, [7047, 0, 4999, -2])          # W, W_SF, Hz, Hz_SF
        self.put(M103 + 36, [4])                          # St = MPPT
        self.put(M120 + 0, [4, 1200, 1])                  # DERTyp, WRtg, WRtg_SF -> 12000 W
        self.put(M121 + 0, [1200])
        self.put(M121 + 20, [1])                          # WMax_SF
        self.put(M123 + 2, [1, 10000])                    # Conn, WMaxLimPct
        self.put(M123 + 7, [0])                           # WMaxLim_Ena
        self.put(M123 + 21, [-2])                         # WMaxLimPct_SF
        self.put(M160 + 0, [-3, -2, 0])                   # DCA_SF, DCV_SF, DCW_SF
        self.put(M160 + 6, [4])                           # N modules
        b = M160 + 8
        self.put(b, [1] + text_regs("MPPT 1", 8) + [14100, 51500, 7272])
        self.put(b + 20, [2] + text_regs("MPPT 2", 8) + [65535, 65535, 65535])
        self.put(b + 40, [3] + text_regs("StCha 3", 8) + [0, 32500, 0])   # battery port, no battery

    def start(self, port=0):
        fake = self

        class Handler(socketserver.BaseRequestHandler):
            def recv(self, n):
                buf = b""
                while len(buf) < n:
                    chunk = self.request.recv(n - len(buf))
                    if not chunk:
                        return None
                    buf += chunk
                return buf

            def handle(self):
                fake.clients.append(self.request)
                while True:
                    head = self.recv(7)
                    if head is None:
                        return
                    tid, _, length, unit = struct.unpack(">HHHB", head)
                    body = self.recv(length - 1)
                    self.request.sendall(struct.pack(">HH", tid, 0) + self.reply(unit, body))

            def reply(self, unit, body):
                fc = body[0]

                def frame(pdu):
                    return struct.pack(">HB", len(pdu) + 1, unit) + pdu

                def exc(code):
                    return frame(bytes([fc | 0x80, code]))

                if fc == 3:
                    addr, count = struct.unpack(">HH", body[1:5])
                    if any(a not in fake.regs for a in range(addr, addr + count)):
                        return exc(2)
                    data = b"".join(struct.pack(">H", fake.regs[a]) for a in range(addr, addr + count))
                    return frame(bytes([3, len(data)]) + data)
                if fc == 6:
                    addr, value = struct.unpack(">HH", body[1:5])
                    fake.writes.append((addr, value))
                    if fake.refuse_writes or addr in fake.refuse or addr not in fake.regs:
                        return exc(3)
                    fake.regs[addr] = value
                    return frame(body[:5])
                return exc(1)

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", port), Handler)
        self._server.daemon_threads = True
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
