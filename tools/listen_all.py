#!/usr/bin/env python3
"""Listen on every audio channel at once and say which one carries the external sound.

For finding out where the other side of a call arrives, before trusting a
recording to it. Opens

* every source the sound server offers (inputs and output monitors), and
* the host bridge listener (the port the Windows sender connects to),

and prints, every couple of seconds, the level of each plus what the bridge is
doing (who connected, which host device it captures, at what rate). At the end -
Ctrl+C, SIGTERM or --seconds - it names the channels that carried sound.

    python3 tools/listen_all.py                 # until Ctrl+C
    python3 tools/listen_all.py --seconds 120   # fixed duration
    python3 tools/listen_all.py --save DIR      # also keep one WAV per channel

Needs nothing but what Echolot already needs. Do not run it while Echolot records:
both want the bridge port (the second one reports it and goes on without it).
"""

from __future__ import annotations

import argparse
import math
import signal
import socket
import sys
import time
import wave
from array import array
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from echolot.audio import bridge, devices  # noqa: E402
from echolot.audio.capture import CaptureProcess  # noqa: E402
from echolot.audio.mixer import measure  # noqa: E402

RATE = 48000
BLOCK = 960
FLOOR = -90.0
#: A channel counts as carrying sound above this (quiet room noise stays below).
SIGNAL_DB = -50.0
WIDTH = 24


def db(value: float) -> float:
    return FLOOR if value <= 0 else max(FLOOR, 20 * math.log10(value / 32768))


def bar(level: float) -> str:
    filled = round(max(0.0, min(1.0, (level - FLOOR) / -FLOOR)) * WIDTH)
    return "#" * filled + "." * (WIDTH - filled)


class Channel:
    def __init__(self, source: devices.Device, save: Path | None) -> None:
        self.source = source
        self.is_bridge = source.name == bridge.DEVICE
        self.window_peak = 0
        self.best_peak = 0
        self.loud_seconds = 0.0
        self.blocks = 0
        self.process = CaptureProcess(
            source.name, side="speaker" if source.is_monitor else "mic",
            sample_rate=RATE, block_frames=BLOCK, buffer_seconds=5.0,
        )
        self.wav: wave.Wave_write | None = None
        if save is not None:
            safe = "".join(c if c.isalnum() else "_" for c in source.name)[:80]
            self.wav = wave.open(str(save / f"{safe}.wav"), "wb")
            self.wav.setnchannels(1)
            self.wav.setsampwidth(2)
            self.wav.setframerate(RATE)

    def start(self) -> bool:
        try:
            self.process.start()
            return True
        except OSError as exc:
            print(f"  cannot open {self.source.name}: {exc}")
            return False

    def pump(self) -> None:
        while True:
            block = self.process.read(timeout=0)
            if block is None:
                return
            samples = array("h")
            samples.frombytes(block)
            peak, _mean = measure(samples)
            self.blocks += 1
            self.window_peak = max(self.window_peak, peak)
            self.best_peak = max(self.best_peak, peak)
            if db(peak) > SIGNAL_DB:
                self.loud_seconds += BLOCK / RATE
            if self.wav is not None:
                self.wav.writeframes(block)

    def take_window(self) -> float:
        level = db(self.window_peak)
        self.window_peak = 0
        return level

    @property
    def label(self) -> str:
        kind = "HOST-STREAM " if self.is_bridge else ("monitor     " if self.source.is_monitor else "input       ")
        return f"{kind}{self.source.label()[:48]}"

    def stop(self) -> None:
        self.process.stop()
        if self.wav is not None:
            self.wav.close()


def guest_addresses() -> list[str]:
    """This machine's addresses as the host would see them (no traffic is sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.168.68.1", 9))
            return [probe.getsockname()[0]]
    except OSError:
        return []


def main() -> int:
    parser = argparse.ArgumentParser(description="Listen on every channel and find the external sound.")
    parser.add_argument("--seconds", type=float, default=0, help="stop after this long (default: Ctrl+C)")
    parser.add_argument("--every", type=float, default=2.0, help="report interval in seconds")
    parser.add_argument("--bridge", action="store_true", help="also listen for the host_send.py stream")
    parser.add_argument("--port", type=int, default=bridge.DEFAULT_PORT)
    parser.add_argument("--save", type=Path, help="write one WAV per channel into this folder")
    args = parser.parse_args()

    bridge.configure("on" if args.bridge else "off", args.port)
    if args.save:
        args.save.mkdir(parents=True, exist_ok=True)

    events: list[str] = []

    def on_bridge(event: str, fields: dict) -> None:
        stamp = time.strftime("%H:%M:%S")
        events.append(f"{stamp}  host stream {event}: {fields}")
        print(f"[{stamp}] HOST STREAM {event.upper()}  {fields}", flush=True)

    bridge.subscribe(on_bridge)

    sources = devices.list_sources()
    print("Sources offered:")
    for source in sources:
        print(f"  {'monitor' if source.is_monitor else 'input  '}  {source.name}  [{source.state or '-'}]")
    print()
    if args.bridge:
        print(f"Waiting for the Windows sender on port {args.port}; this machine is {', '.join(guest_addresses()) or '?'}")
    print("Play something in Teams now. Reporting every %.0f s.\n" % args.every, flush=True)

    channels = [c for c in (Channel(s, args.save) for s in sources) if c.start()]
    if args.bridge and not any(c.is_bridge for c in channels):
        print("  (host bridge not available - is the port taken, e.g. by a running Echolot?)\n")

    stop = {"now": False}
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.update(now=True))

    started = last = time.monotonic()
    try:
        while not stop["now"]:
            time.sleep(0.1)
            for channel in channels:
                channel.pump()
            now = time.monotonic()
            if args.seconds and now - started >= args.seconds:
                break
            if now - last < args.every:
                continue
            last = now
            print(f"--- {now - started:5.0f} s   bridge: {bridge.status()}" + (f" {bridge.HUB.info}" if bridge.HUB.info else ""))
            for channel in channels:
                level = channel.take_window()
                mark = "  <== SOUND" if level > SIGNAL_DB else ""
                print(f"  {channel.label:<62} {bar(level)} {level:6.1f} dB{mark}")
            sys.stdout.flush()
    finally:
        for channel in channels:
            channel.pump()
            channel.stop()

    print("\n=== RESULT after %.0f s ===" % (time.monotonic() - started))
    carrying = [c for c in channels if db(c.best_peak) > SIGNAL_DB]
    for channel in sorted(channels, key=lambda c: -c.best_peak):
        print(f"  {channel.label:<62} peak {db(channel.best_peak):6.1f} dB   sound for {channel.loud_seconds:5.1f} s")
    print()
    bridge_channel = next((c for c in channels if c.is_bridge), None)
    if bridge_channel and db(bridge_channel.best_peak) > SIGNAL_DB:
        print("EXTERNAL SOUND ARRIVES VIA THE HOST STREAM:", bridge_channel.source.label())
    elif bridge_channel and (bridge.HUB.info or any("connected" in e for e in events)):
        print("The host stream connected but stayed silent: wrong playback device on the host?")
    elif bridge_channel:
        print("The host stream NEVER connected: host_send.py is not running, or cannot reach this machine.")
    for channel in carrying:
        if not channel.is_bridge:
            kind = ("output monitor (sound played inside THIS VM)" if channel.source.is_monitor
                    else "input (what VMware passes in from the host)")
            print(f"SOUND ON {kind}: {channel.source.label()}")
    if not carrying:
        print("No channel carried any sound at all.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
