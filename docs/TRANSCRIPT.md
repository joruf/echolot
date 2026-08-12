# Building a Transcript from an Echolot Recording

Echolot writes the two files a transcript needs:

```
Echolot_2026-08-12_10-15-03.opus     the audio
Echolot_2026-08-12_10-15-03.log      who spoke when, in seconds from the file's start
```

Speaker attribution never needs a diarisation model here, because Echolot measured both sides
separately while recording. In the default **mixed** layout that information lives in the log; in the
**split** layout it is additionally in the audio itself, one voice per channel.

Pick the path that matches how the file was recorded — the log's first line says which:

```bash
BASE=~/Downloads/Echolot/Echolot_2026-08-12_10-15-03
head -1 "$BASE.log" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["layout"], d["preroll_seconds"], "s pre-roll")'
```

If `preroll_seconds` is greater than zero, the file starts *before* the recording was triggered. The
log marks that moment:

```jsonl
{"type":"recording_started","t":120.0}
```

Everything before `t` is pre-roll. It is normal audio and transcribes like the rest — it is just
worth knowing, because that is usually where the sentence you actually wanted sits.

The scripts below label the speakers in German (`Ich` / `Gegenüber`) because that is the language
these transcripts are usually read in; change the `LABELS` dictionary and the `--language` flag to
match your conversations.

---

## Path A — mixed recording (the default)

Transcribe the single track once, then label each line using the log.

### 1. Transcribe

```bash
ffmpeg -i "$BASE.opus" -ar 16000 -ac 1 "$BASE.wav"
whisper "$BASE.wav" --language de --model medium --output_format json
```

Or, faster, with [faster-whisper](https://github.com/SYSTRAN/faster-whisper):

```python
from faster_whisper import WhisperModel

model = WhisperModel("medium", device="cpu", compute_type="int8")
segments, _ = model.transcribe(f"{BASE}.wav", language="de", vad_filter=True)
result = {"segments": [{"start": s.start, "end": s.end, "text": s.text} for s in segments]}
```

### 2. Label each line from the log

Every recognised segment gets the speaker whose logged speech overlaps it most.

```python
#!/usr/bin/env python3
"""Turn a whisper transcript of a mixed Echolot recording into a labelled dialogue.

    python3 zuordnen.py .../Echolot_2026-08-12_10-15-03
expects  <base>.json  (whisper output)  and  <base>.log  (Echolot)
writes   <base>.transkript.md
"""
import json
import sys
from pathlib import Path

LABELS = {"mic": "Ich", "speaker": "Gegenüber"}


def load_speech(log_path: Path) -> list[dict]:
    """The speech segments Echolot detected, sorted by start time."""
    segments = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        if entry.get("type") == "speech":
            segments.append(entry)
    segments.sort(key=lambda item: item["start"])
    return segments


def speaker_for(start: float, end: float, speech: list[dict]) -> str:
    """Whoever was talking for the longest part of this stretch."""
    overlap = {"mic": 0.0, "speaker": 0.0}
    for segment in speech:
        if segment["end"] <= start:
            continue
        if segment["start"] >= end:
            break  # sorted, so nothing later can overlap either
        shared = min(end, segment["end"]) - max(start, segment["start"])
        if shared > 0:
            overlap[segment["src"]] = overlap.get(segment["src"], 0.0) + shared
    if overlap["mic"] == 0.0 and overlap["speaker"] == 0.0:
        return "unklar"
    return "mic" if overlap["mic"] >= overlap["speaker"] else "speaker"


def timestamp(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


base = Path(sys.argv[1])
speech = load_speech(base.with_suffix(".log"))
whisper = json.loads(base.with_suffix(".json").read_text(encoding="utf-8"))

lines = []
for segment in whisper.get("segments", []):
    text = segment.get("text", "").strip()
    if not text:
        continue
    who = speaker_for(segment["start"], segment["end"], speech)
    lines.append((segment["start"], LABELS.get(who, "Unklar"), text))

out = base.with_suffix(".transkript.md")
with out.open("w", encoding="utf-8") as stream:
    stream.write(f"# Gespräch {base.name}\n\n")
    previous = None
    for start, who, text in lines:
        if who != previous:
            stream.write(f"\n**{who}** _{timestamp(start)}_\n\n")
            previous = who
        stream.write(f"{text}\n")
print(out)
```

Where this is exact and where it is not: as long as people take turns — which is how conversations
mostly go — the overlap is unambiguous. While both talk at once, one label wins and the other's words
end up under it. If that matters for your material, record in the split layout.

## Path B — split recording (two channels)

Here the channel *is* the speaker, so each side is transcribed on its own and nothing has to be
attributed afterwards.

```bash
ffmpeg -i "$BASE.opus" -filter_complex "pan=mono|c0=c0" -ar 16000 "$BASE.ich.wav"
ffmpeg -i "$BASE.opus" -filter_complex "pan=mono|c0=c1" -ar 16000 "$BASE.gegenueber.wav"

whisper "$BASE.ich.wav"        --language de --model medium --output_format json
whisper "$BASE.gegenueber.wav" --language de --model medium --output_format json
```

Two separate runs, never one on the mixed-down file: that would throw away exactly the information
this layout exists to keep.

```python
#!/usr/bin/env python3
"""Merge two whisper JSON outputs into one dialogue, ordered by time."""
import json
import sys
from pathlib import Path

LABELS = {"ich": "Ich", "gegenueber": "Gegenüber"}


def load(path: Path, speaker: str) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [
        {"start": segment["start"], "speaker": speaker, "text": segment["text"].strip()}
        for segment in data.get("segments", [])
        if segment.get("text", "").strip()
    ]


base = Path(sys.argv[1])
lines = load(base.with_suffix(".ich.json"), "ich")
lines += load(base.with_suffix(".gegenueber.json"), "gegenueber")
lines.sort(key=lambda entry: entry["start"])

for entry in lines:
    minutes, secs = divmod(int(entry["start"]), 60)
    print(f"[{minutes:02d}:{secs:02d}] {LABELS[entry['speaker']]}: {entry['text']}")
```

## The speaker timeline on its own

Useful without any speech recognition — who talked when, and how much:

```python
#!/usr/bin/env python3
"""Read an Echolot log and print the speaker timeline."""
import json
import sys
from pathlib import Path

LABELS = {"mic": "Ich", "speaker": "Gegenüber"}

path = Path(sys.argv[1])  # .../Echolot_2026-08-12_10-15-03.log
header, segments, events = {}, [], []
for line in path.read_text(encoding="utf-8").splitlines():
    entry = json.loads(line)
    kind = entry.get("type")
    if kind == "session":
        header = entry
    elif kind == "speech":
        segments.append(entry)
    elif kind == "session_end":
        header["end"] = entry
    else:
        events.append(entry)

segments.sort(key=lambda item: item["start"])  # written when they end, so sort
print(f"{header.get('audio')}  {header.get('layout')}  gestartet {header.get('started_at')}")
for segment in segments:
    print(
        f"  {segment['start']:8.2f} - {segment['end']:8.2f}  "
        f"{LABELS.get(segment['src'], segment['src']):10} {segment['duration']:5.2f}s"
    )

totals = header.get("end", {}).get("speech_seconds", {})
print(f"\nRedeanteil: ich {totals.get('mic', 0):.0f}s, Gegenüber {totals.get('speaker', 0):.0f}s")
for event in events:
    print(f"! {event['type']} bei {event.get('t', 0):.2f}s")
```

Cutting one answer out of the recording to listen again:

```bash
ffmpeg -i "$BASE.opus" -ss 15.62 -t 6.22 antwort.wav                       # mixed
ffmpeg -i "$BASE.opus" -filter_complex "pan=mono|c0=c1" -ss 15.62 -t 6.22 antwort.wav   # split
```

## Check the recording before trusting the transcript

The last line of the log answers whether anything was missed:

```bash
tail -1 "$BASE.log" | python3 -m json.tool
```

| Field | What it tells you |
|-------|-------------------|
| `speech_seconds` | how much each side actually said. A `0.0` for one side means that side was silent — check the recording before transcribing anything. |
| `preroll_written` | whether the buffered lead-up made it into the file completely (`complete: true`). |
| `gap_blocks` | filled-in blocks per side, 20 ms each. High values point at an outage; the `source_error` entries say when. |
| `clipped_blocks` | blocks where both sides were loud at once and the mono sum had to be limited. A few are harmless. |
| `restarts` / `overruns` | capture processes that had to be restarted, and audio dropped because the machine could not keep up. Normally `0`. |
| `encoder_returncode` | `0` means ffmpeg finished the file cleanly. |
| `duration` vs. the file's length | should match; both come from the same block counter. |

Events like `source_error`, `device_change` or `side_silent_at_start` mark the exact seconds where
something was wrong, which is also where a transcript is most likely to have holes.
