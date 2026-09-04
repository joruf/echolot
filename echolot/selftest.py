"""Proof that both directions are actually recorded - not that nothing crashed.

The one question that matters before an important call is whether *your voice*
and *the other person's voice* both end up in the file, on their own channels.
Nothing short of a real recording answers that, so this is a real recording:

    1. two virtual devices are created, one standing in for a microphone and one
       for what is being played
    2. a different tone goes into each - 440 Hz for the microphone side, 880 Hz
       for the other side
    3. Echolot records them exactly as it would record a conversation
    4. the file is analysed per channel: each channel must carry its own tone and
       not the other one

Step 4 is what makes this worth having. A test that only checks "there is sound"
passes with both sides swapped, or with one side bleeding into both channels; a
test that looks for a specific frequency in a specific channel cannot.

A virtual microphone is used because a real one cannot be fed a known signal.
The path being proven is identical: a source is opened, blocks are read, they are
placed on a channel and encoded.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import time
import wave
from array import array
from dataclasses import dataclass, field
from pathlib import Path

MIC_HZ = 440
OTHER_HZ = 880
#: Its own tone must be this much louder than the other side's for a channel to
#: count as clean. 12 dB is four times the amplitude - far beyond any bleed.
MIN_SEPARATION_DB = 12.0
#: Below this a channel is treated as carrying nothing at all.
MIN_LEVEL_DB = -45.0
FULL_SCALE = 32768.0
PACTL_TIMEOUT = 6
TOOLS = ("pactl", "paplay", "parec")


@dataclass
class SideCheck:
    """What one channel of the finished recording turned out to contain."""

    side: str
    own_db: float
    other_db: float
    speech_seconds: float
    devices: tuple[str, ...] = ()
    #: True when both sides share one channel ("mix"), where separation cannot
    #: be asked for - only that the side is present at all.
    mixed: bool = False

    @property
    def separation_db(self) -> float:
        return self.own_db - self.other_db

    @property
    def heard(self) -> bool:
        return self.own_db >= MIN_LEVEL_DB

    @property
    def clean(self) -> bool:
        """Its own tone, loud enough, and - on its own channel - only its own."""
        if not self.heard:
            return False
        return self.mixed or self.separation_db >= MIN_SEPARATION_DB

    def reason(self) -> str:
        if not self.heard:
            return "nothing arrived for this side"
        if not self.mixed and self.separation_db < MIN_SEPARATION_DB:
            return (
                f"the other side bleeds into this channel "
                f"(separation only {self.separation_db:.0f} dB)"
            )
        return ""


@dataclass
class SelfTestResult:
    ok: bool = False
    checks: list[SideCheck] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    recording: Path | None = None

    def check(self, side: str) -> SideCheck | None:
        return next((entry for entry in self.checks if entry.side == side), None)


# -- signal generation and analysis --------------------------------------


def write_sine(path: Path, hz: float, seconds: float, rate: int = 48000) -> Path:
    """A plain mono sine as a WAV file, written without any outside tool."""
    frames = int(rate * seconds)
    # A little headroom: full scale would clip once two sides are summed.
    amplitude = int(0.5 * (FULL_SCALE - 1))
    samples = array(
        "h", (int(amplitude * math.sin(2 * math.pi * hz * index / rate)) for index in range(frames))
    )
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.tobytes())
    return path


def tone_db(samples: array, hz: float, rate: int) -> float:
    """Level of one frequency in dBFS, via a Goertzel filter.

    Goertzel rather than a full transform because a single known frequency is
    all that is asked about, and this stays exact without a numeric library.
    """
    count = len(samples)
    if count == 0:
        return -math.inf
    omega = 2.0 * math.pi * hz / rate
    coefficient = 2.0 * math.cos(omega)
    first = second = 0.0
    for value in samples:
        first, second = value + coefficient * first - second, first
    magnitude = math.sqrt(first * first + second * second - coefficient * first * second)
    # Normalised so a full-scale sine at this frequency reads 0 dBFS.
    normalised = 2.0 * magnitude / (count * FULL_SCALE)
    if normalised <= 1e-12:
        return -math.inf
    return 20.0 * math.log10(normalised)


def channels_of(path: Path) -> tuple[array, array, int]:
    """Left and right samples of a stereo WAV, plus its sample rate."""
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        count = handle.getnchannels()
        raw = handle.readframes(handle.getnframes())
    interleaved = array("h")
    interleaved.frombytes(raw)
    if count == 1:
        return interleaved, array("h"), rate
    left = array("h", interleaved[0::count])
    right = array("h", interleaved[1::count])
    return left, right, rate


# -- the virtual devices -------------------------------------------------


class VirtualDevices:
    """Two null sinks, so a known signal can be put where a device would be.

    A null sink's monitor is a source like any other, which is what makes it a
    stand-in for a microphone: something can be played into it, and Echolot reads
    it exactly as it reads real hardware. Unloaded again on the way out, always -
    a test must not leave devices behind on the machine.
    """

    def __init__(self, prefix: str = "echolot_selftest") -> None:
        self.prefix = prefix
        self.modules: list[str] = []
        self.mic_sink = f"{prefix}_mic"
        self.other_sink = f"{prefix}_other"

    @property
    def mic_source(self) -> str:
        return f"{self.mic_sink}.monitor"

    @property
    def other_source(self) -> str:
        return f"{self.other_sink}.monitor"

    def __enter__(self) -> VirtualDevices:
        for name, description in ((self.mic_sink, "EcholotSelfTestMic"), (self.other_sink, "EcholotSelfTestOther")):
            result = subprocess.run(
                [
                    "pactl",
                    "load-module",
                    "module-null-sink",
                    f"sink_name={name}",
                    f"sink_properties=device.description={description}",
                ],
                capture_output=True,
                text=True,
                timeout=PACTL_TIMEOUT,
                check=False,
            )
            if result.returncode != 0:
                self.__exit__(None, None, None)
                raise OSError(f"could not create the virtual device {name}: {result.stderr.strip()}")
            self.modules.append(result.stdout.strip())
        # The sinks need a moment before their monitors can be read.
        time.sleep(0.4)
        return self

    def __exit__(self, *_exc) -> None:
        for module in reversed(self.modules):
            subprocess.run(
                ["pactl", "unload-module", module],
                capture_output=True,
                timeout=PACTL_TIMEOUT,
                check=False,
            )
        self.modules.clear()

    def play(self, sink: str, path: Path) -> subprocess.Popen:
        return subprocess.Popen(
            ["paplay", "--device", sink, str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def missing_tools() -> list[str]:
    return [tool for tool in TOOLS if shutil.which(tool) is None]


def sound_server_available() -> bool:
    try:
        result = subprocess.run(
            ["pactl", "info"], capture_output=True, timeout=PACTL_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


# -- the test itself -----------------------------------------------------


def run_pipeline_test(
    seconds: float = 5.0,
    workdir: Path | None = None,
    layout: str = "split",
    audio_format: str = "wav",
) -> SelfTestResult:
    """Record both directions through the real pipeline and check the result.

    Everything is set up from scratch - devices, settings, recording - so the
    outcome says something about Echolot rather than about the machine's current
    configuration. WAV analyses the samples exactly as they were written; opus and
    flac are decoded back first, so the format people actually record in is proven
    rather than assumed - a lossy codec still has to deliver both sides.
    """
    from .config import Config
    from .session import Recorder, State
    from .speechlog import MIC, SPEAKER

    result = SelfTestResult()

    absent = missing_tools()
    if absent:
        result.problems.append(f"missing tools: {', '.join(absent)}")
        return result
    if not sound_server_available():
        result.problems.append("no sound server answered (pactl info failed)")
        return result

    own = workdir is None
    base = Path(workdir) if workdir is not None else Path(
        __import__("tempfile").mkdtemp(prefix="echolot-selftest-")
    )
    recordings = base / "recordings"
    recordings.mkdir(parents=True, exist_ok=True)

    try:
        with VirtualDevices() as virtual:
            mic_tone = write_sine(base / "mic.wav", MIC_HZ, seconds + 2.0)
            other_tone = write_sine(base / "other.wav", OTHER_HZ, seconds + 2.0)

            config = Config(path=base / "settings.json")
            config.set("recordings_dir", str(recordings))
            config.set("audio.format", audio_format)
            # "split" keeps the sides on their own channels, which is what makes
            # a per-side statement possible; "mix" sums them into one channel and
            # can only be asked whether both are in there.
            config.set("audio.layout", layout)
            config.set("devices.mic", [virtual.mic_source])
            config.set("devices.speaker", [virtual.other_source])
            config.set("preroll.minutes", 0)
            config.set("devices.follow_default", False)
            config.set("notifications.on_start", False)
            config.set("notifications.on_stop", False)
            config.validate()

            recorder = Recorder(config)
            if not recorder.start():
                result.problems.append(recorder.last_error or "the recording did not start")
                return result

            players = [
                virtual.play(virtual.mic_sink, mic_tone),
                virtual.play(virtual.other_sink, other_tone),
            ]
            deadline = time.monotonic() + seconds
            while recorder.state is State.RECORDING and time.monotonic() < deadline:
                time.sleep(0.1)
            recorder.stop()
            for player in players:
                player.terminate()

            files = recorder.files
            if files is None or not Path(files.audio).exists():
                result.problems.append("no file was written")
                return result
            result.recording = Path(files.audio)

            analysable = _as_wav(Path(files.audio), base)
            if analysable is None:
                result.problems.append(f"the {audio_format} file could not be decoded back")
                return result
            left, right, rate = channels_of(analysable)
            mixed = not right
            if mixed and layout == "split":
                result.problems.append("split was asked for but the file has one channel")
                return result

            spoken = _speech_seconds_from_log(Path(files.log))
            for side, samples, own_hz, other_hz in (
                (MIC, left, MIC_HZ, OTHER_HZ),
                (SPEAKER, left if mixed else right, OTHER_HZ, MIC_HZ),
            ):
                result.checks.append(
                    SideCheck(
                        side=side,
                        own_db=tone_db(samples, own_hz, rate),
                        other_db=tone_db(samples, other_hz, rate),
                        speech_seconds=float(spoken.get(side, 0.0)),
                        devices=(virtual.mic_source,) if side == MIC else (virtual.other_source,),
                        mixed=mixed,
                    )
                )

            result.ok = bool(result.checks) and all(check.clean for check in result.checks)
            for check in result.checks:
                if not check.clean:
                    result.problems.append(f"{check.side}: {check.reason()}")
            return result
    except OSError as exc:
        result.problems.append(str(exc))
        return result
    finally:
        if own and result.recording is None:
            shutil.rmtree(base, ignore_errors=True)


def _as_wav(path: Path, base: Path) -> Path | None:
    """The recording as a WAV that can be analysed, decoding if it has to be."""
    if path.suffix.lower() == ".wav":
        return path
    target = base / "decoded.wav"
    result = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-c:a", "pcm_s16le", str(target)],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not target.exists():
        return None
    return target


def _speech_seconds_from_log(path: Path) -> dict[str, float]:
    """What the speech detection made of it, straight from the session log."""
    import json

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("type") == "session_end":
            found = entry.get("speech_seconds") or {}
            return {str(k): float(v) for k, v in found.items()}
    return {}


def check_configured_devices(config, seconds: float = 2.0) -> list[tuple[str, str, float]]:
    """Measure what the devices that would really be used are delivering.

    Returns (side, device, dBFS) per device. Exact digital silence comes back as
    -inf, which is the difference between "quiet room" and "nothing connected" -
    the distinction that decides whether a conversation gets recorded.
    """
    from .audio import devices as devices_module
    from .speechlog import MIC, SPEAKER

    resolution = devices_module.resolve(
        config.get("devices.mic"), config.get("devices.speaker")
    )
    out: list[tuple[str, str, float]] = []
    for side, names in ((MIC, resolution.mics), (SPEAKER, resolution.speakers)):
        for name in names:
            out.append((side, name, _peak_db(name, seconds)))
    return out


def _peak_db(device: str, seconds: float) -> float:
    """Loudest sample on a device over a short listen, in dBFS."""
    try:
        process = subprocess.Popen(
            [
                "parec",
                f"--device={device}",
                "--format=s16le",
                "--rate=48000",
                "--channels=1",
                "--raw",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return -math.inf
    try:
        wanted = int(48000 * 2 * seconds)
        chunks: list[bytes] = []
        got = 0
        deadline = time.monotonic() + seconds + 2.0
        while got < wanted and time.monotonic() < deadline:
            chunk = process.stdout.read(4096) if process.stdout else b""
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)
    finally:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()

    raw = b"".join(chunks)
    if len(raw) < 2:
        return -math.inf
    samples = array("h")
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    peak = max(abs(value) for value in samples) if samples else 0
    if peak == 0:
        return -math.inf
    return 20.0 * math.log10(peak / FULL_SCALE)
