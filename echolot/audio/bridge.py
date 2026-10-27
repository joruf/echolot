"""The host bridge: the other side's audio, streamed in from the machine around the guest.

Inside a virtual machine the sound card carries what the guest plays and nothing
else, so a meeting running on the *host* is silent here no matter what the guest
is told to record. The way out is a small sender on the host (`host_send.py`,
WASAPI loopback, no administrator rights) that connects to this machine and pushes
what the host is playing as raw PCM. This module is the receiving end.

The contract, as the sender implements it:

* the sender runs permanently and tries to connect to our port about twice a
  second; **a listener that is open means "recording"**, a closed one means "idle"
* first one JSON line `{"rate", "channels", "format": "s16le", "device",
  "host_time"}`, then raw interleaved s16le PCM until the connection ends
* while nothing plays the sender pads with zeros, so the stream is on the wall
  clock: N seconds of connection are N seconds of audio

To the rest of Echolot this is one more capturable source, a pseudo device called
`DEVICE`. It shows up in the output list, is recorded by "record everything",
can be ticked in the levels dialog and is supervised, summed and logged like any
monitor. Only the reader behind it is different: a TCP listener instead of a
child process.

The listener exists only while somebody reads from the bridge, so nothing can
connect to the port while Echolot is idle (unless the pre-roll is on, which keeps
every source open on purpose). Only private and loopback peers are accepted.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import sys
import threading
import time
from array import array
from typing import Callable

from .. import virt
from ..i18n import t
from .backend import Device

#: Name of the pseudo device in source lists, config files and the speech log.
DEVICE = "echolot-host-bridge"
DEFAULT_PORT = 8765
MODES = ("auto", "on", "off")

#: How long a host may take to send its header before it is given up on.
HEADER_TIMEOUT = 3.0
HEADER_LIMIT = 4096
#: A connected host that delivers nothing for this long is treated like a
#: missing one: silence is written so the timeline keeps moving.
STALL_SECONDS = 0.5
#: Audio queued for a slow reader. Only a stalled mixer gets here.
MAX_BUFFER_SECONDS = 60.0
#: Never write more than this much catch-up silence at once.
MAX_CATCHUP_SECONDS = 1.0
SANE_RATES = range(8000, 192001)
SANE_CHANNELS = range(1, 9)


class _Settings:
    mode = "off"
    port = DEFAULT_PORT


def configure(mode: str, port: int = DEFAULT_PORT) -> None:
    """Apply the `bridge` settings. Until this is called the bridge is off."""
    _Settings.mode = mode if mode in MODES else "auto"
    try:
        port = int(port)
    except (TypeError, ValueError):
        port = DEFAULT_PORT
    _Settings.port = port if 1024 <= port <= 65535 else DEFAULT_PORT


def port() -> int:
    return _Settings.port


def enabled() -> bool:
    """Whether the bridge is offered as a source on this machine.

    `auto` means "inside VMware", which is the one place the host sender exists
    for; on real hardware the port would only be open for nothing.
    """
    if _Settings.mode == "on":
        return True
    return _Settings.mode == "auto" and virt.hypervisor() == "vmware"


def device() -> Device:
    """The pseudo device as a source list shows it."""
    return Device(name=DEVICE, description=t("bridge.device_label"), is_monitor=True, state="")


# -- format conversion ----------------------------------------------------


def downmix(samples: array, channels: int) -> array:
    """Interleaved frames to mono, averaging the channels."""
    if channels == 1:
        return samples
    if channels == 2:
        return array("h", [(a + b) >> 1 for a, b in zip(samples[0::2], samples[1::2])])
    lanes = [samples[lane::channels] for lane in range(channels)]
    return array("h", [sum(frame) // channels for frame in zip(*lanes)])


class Resampler:
    """Streaming mono resampler: linear interpolation up, area average down.

    Neither is studio quality, and neither has to be: this is speech for a log
    and a recording, and the alternative - a second ffmpeg per connection - would
    add a process, a buffer and a start-up delay to a path that must not stall.
    """

    def __init__(self, source_rate: int, target_rate: int) -> None:
        self.step = source_rate / target_rate
        self._tail: list[int] = []
        self._pos = 0.0

    def process(self, samples: array) -> array:
        if self.step == 1.0:
            return samples
        data = self._tail + samples.tolist()
        out: list[int] = []
        pos, step, size = self._pos, self.step, len(data)
        if step < 1.0:
            while pos + 1 < size:
                i = int(pos)
                out.append(int(data[i] + (data[i + 1] - data[i]) * (pos - i)))
                pos += step
        else:
            while pos + step <= size:
                a, b = int(pos), max(int(pos) + 1, int(pos + step))
                out.append(sum(data[a:b]) // (b - a))
                pos += step
        keep = int(pos)
        self._tail = data[keep:]
        self._pos = pos - keep
        return array("h", out)


# -- the hub: one listener, any number of readers -------------------------


class Hub:
    """Owns the listening socket and the one connection from the host.

    Several readers can be open at once (the levels dialog next to a recording,
    a pre-roll handing over), so the audio is fanned out rather than owned by
    whoever opened the port first.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._readers: list[BridgeReader] = []
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._conn: socket.socket | None = None
        self._stop = threading.Event()
        self._subscribers: list[Callable[[str, dict], None]] = []
        #: waiting | connected | no_header, only meaningful while open
        self.state = "waiting"
        self.info: dict = {}

    # -- observers --------------------------------------------------------

    def subscribe(self, callback: Callable[[str, dict], None]) -> Callable[[], None]:
        """Be told about connections; returns the function that unsubscribes."""
        with self._lock:
            self._subscribers.append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)

        return unsubscribe

    def _emit(self, event: str, **fields) -> None:
        with self._lock:
            callbacks = list(self._subscribers)
        for callback in callbacks:
            try:
                callback(event, fields)
            except Exception:  # noqa: BLE001 - an observer must never kill the receiver
                pass

    @property
    def is_open(self) -> bool:
        return self._server is not None

    @property
    def connected(self) -> bool:
        return self.is_open and self.state == "connected"

    # -- readers ----------------------------------------------------------

    def acquire(self, reader: "BridgeReader") -> None:
        """Register a reader; the first one opens the listener. Raises OSError."""
        with self._lock:
            if self._server is None:
                self._open()
            self._readers.append(reader)
            if self.state == "connected":
                reader._begin(self.info["rate"], self.info["channels"])

    def release(self, reader: "BridgeReader") -> None:
        with self._lock:
            if reader in self._readers:
                self._readers.remove(reader)
            if not self._readers:
                self._close()

    def _open(self) -> None:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("0.0.0.0", port()))
            server.listen(2)
            server.settimeout(0.25)
        except OSError:
            server.close()
            raise
        self._server = server
        self._stop = threading.Event()
        self.state = "waiting"
        self.info = {}
        self._thread = threading.Thread(
            target=self._serve, args=(server, self._stop), name="echolot-bridge", daemon=True
        )
        self._thread.start()

    def _close(self) -> None:
        self._stop.set()
        server, self._server = self._server, None
        conn, self._conn = self._conn, None
        for sock in (conn, server):
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
        self.state = "waiting"
        self.info = {}

    # -- the receiving side -----------------------------------------------

    def _serve(self, server: socket.socket, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                conn, address = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not _is_local(address[0]):
                conn.close()
                self._emit("rejected", peer=address[0])
                continue
            with self._lock:
                previous, self._conn = self._conn, conn
            if previous is not None:
                # The host starts over (device change, restart): the new one wins.
                try:
                    previous.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            threading.Thread(
                target=self._receive,
                args=(conn, address[0], stop),
                name="echolot-bridge-rx",
                daemon=True,
            ).start()

    def _receive(self, conn: socket.socket, peer: str, stop: threading.Event) -> None:
        header = _read_header(conn)
        if isinstance(header, str):
            # Closed before the header: the sender found no headset to listen to.
            with self._lock:
                if self._conn is conn:
                    self.state, self.info = "no_header", {"reason": header, "peer": peer}
            self._emit("no_headset" if header == "closed" else "bad_header", peer=peer)
            conn.close()
            return

        rate, channels = header["rate"], header["channels"]
        info = {
            "rate": rate,
            "channels": channels,
            "device": str(header.get("device", "")),
            "peer": peer,
            "host_clock_offset_s": _clock_offset(header.get("host_time")),
        }
        with self._lock:
            if self._conn is not conn:
                conn.close()
                return
            self.state, self.info = "connected", info
            for reader in self._readers:
                reader._begin(rate, channels)
        self._emit("connected", **info)

        started = time.monotonic()
        frame_bytes = 2 * channels
        rest = b""
        conn.settimeout(1.0)
        try:
            while not stop.is_set():
                try:
                    chunk = conn.recv(65536)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                rest += chunk
                usable = len(rest) - len(rest) % frame_bytes
                if usable == 0:
                    continue
                payload, rest = rest[:usable], rest[usable:]
                with self._lock:
                    readers = list(self._readers)
                for reader in readers:
                    reader._feed(payload)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
            with self._lock:
                ours = self._conn is conn
                if ours:
                    self._conn = None
                    self.state, self.info = "waiting", {}
                    for reader in self._readers:
                        reader._end()
            if ours and not stop.is_set():
                self._emit("disconnected", seconds=round(time.monotonic() - started, 1))


def _is_local(host: str) -> bool:
    """Only the machine itself and private networks may send audio in."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


def _clock_offset(host_time) -> float | None:
    try:
        return round(float(host_time) - time.time(), 2)
    except (TypeError, ValueError):
        return None


def _read_header(conn: socket.socket) -> dict | str:
    """The header dict, or why there is none: "closed", "timeout" or "invalid"."""
    conn.settimeout(HEADER_TIMEOUT)
    buffer = b""
    try:
        while not buffer.endswith(b"\n"):
            if len(buffer) >= HEADER_LIMIT:
                return "invalid"
            byte = conn.recv(1)
            if not byte:
                return "closed"
            buffer += byte
    except socket.timeout:
        return "timeout"
    except OSError:
        return "closed"
    try:
        header = json.loads(buffer)
        rate, channels = int(header["rate"]), int(header["channels"])
    except (ValueError, KeyError, TypeError):
        return "invalid"
    if header.get("format", "s16le") != "s16le" or rate not in SANE_RATES or channels not in SANE_CHANNELS:
        return "invalid"
    header["rate"], header["channels"] = rate, channels
    return header


HUB = Hub()


def subscribe(callback: Callable[[str, dict], None]) -> Callable[[], None]:
    return HUB.subscribe(callback)


def status() -> str:
    """`off`, `waiting`, `connected` or `no_header` - what the tooltip may say."""
    return HUB.state if HUB.is_open else "off"


# -- the reader the capture code uses --------------------------------------


class BridgeReader:
    """Mono s16 PCM at the recording rate, shaped like a pipe from `parec`.

    It never ends by itself and never leaves the timeline short: without a host
    it hands out silence at the pace of the wall clock, with one it hands out
    the host's audio, so the mixer sees an ordinary monitor that is quiet.
    """

    def __init__(self, hub: Hub, *, sample_rate: int) -> None:
        self._hub = hub
        self.sample_rate = sample_rate
        self.dropped_bytes = 0
        self._cond = threading.Condition()
        self._buffer = bytearray()
        self._closed = False
        self._live = False
        self._wait_since: float | None = None
        self._due: float | None = None
        self._resampler: Resampler | None = None
        self._channels = 1
        self._max_bytes = int(MAX_BUFFER_SECONDS * sample_rate) * 2
        hub.acquire(self)

    # -- fed by the hub ---------------------------------------------------

    def _begin(self, rate: int, channels: int) -> None:
        with self._cond:
            self._resampler = Resampler(rate, self.sample_rate)
            self._channels = channels
            self._live = True
            self._wait_since = None
            self._cond.notify_all()

    def _end(self) -> None:
        with self._cond:
            self._live = False
            self._cond.notify_all()

    def _feed(self, payload: bytes) -> None:
        samples = array("h")
        samples.frombytes(payload)
        if sys.byteorder == "big":
            samples.byteswap()
        with self._cond:
            resampler = self._resampler
            if resampler is None:
                return
            mono = resampler.process(downmix(samples, self._channels))
            if sys.byteorder == "big":
                mono.byteswap()
            self._buffer += mono.tobytes()
            overflow = len(self._buffer) - self._max_bytes
            if overflow > 0:
                del self._buffer[:overflow]
                self.dropped_bytes += overflow
            self._cond.notify_all()

    # -- the Reader interface ---------------------------------------------

    def read(self, count: int) -> bytes:
        with self._cond:
            while True:
                if self._closed:
                    return b""
                if self._buffer:
                    data = bytes(self._buffer[:count])
                    del self._buffer[:count]
                    self._due = self._wait_since = None
                    return data
                now = time.monotonic()
                if self._live:
                    # The host is on the wall clock; give a late chunk a moment
                    # before writing silence over it.
                    if self._wait_since is None:
                        self._wait_since = now
                    remaining = self._wait_since + STALL_SECONDS - now
                    if remaining > 0:
                        self._cond.wait(remaining)
                        continue
                if self._due is None or now - self._due > MAX_CATCHUP_SECONDS:
                    self._due = now
                if self._due > now:
                    self._cond.wait(self._due - now)
                    continue
                self._due += count / 2 / self.sample_rate
                return bytes(count)

    @property
    def alive(self) -> bool:
        return not self._closed

    @property
    def returncode(self) -> int | None:
        return 0 if self._closed else None

    def close(self, timeout: float = 1.0) -> None:  # noqa: ARG002 - Reader interface
        with self._cond:
            if self._closed:
                return
            self._closed = True
            self._cond.notify_all()
        self._hub.release(self)


def open_reader(*, sample_rate: int) -> BridgeReader:
    """Open the listener and read from it. Raises OSError when the port is taken."""
    return BridgeReader(HUB, sample_rate=sample_rate)
