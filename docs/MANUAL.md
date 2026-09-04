# Echolot — User Manual

Echolot sits in the system tray and records conversations: your voice and the other side's, mixed
into one recording, plus a log of who spoke when.

The interface comes in **English** (default), **German**, **Spanish** and **French**. This manual
uses the English labels; switch the language under *Settings → Language* and the same items appear in
yours.

---

## 1. The tray icon

After login the icon sits in the panel and does nothing until you tell it to.

| Icon | Meaning |
|------|---------|
| blue sonar | ready, nothing is being recorded |
| **blinking red** | recording |
| amber with pause bars | paused |
| red with `!` | a problem - hover for the tooltip |

**Double click** starts the recording and stops it again. A single click does nothing on purpose,
so a stray click can neither start nor - much worse - stop a running recording.

Hover the icon while recording and the tooltip shows the elapsed time, both levels, the devices in
use, the file name and the free space.

## 2. The menu (right click)

```
Echolot – Recording · 12:34
──────────────────────────────────
Stop recording
Pause
──────────────────────────────────
You         ▮▮▮▮▮▯▯▯▯▯▯▯    -28 dB
Other side  ▮▮▮▮▮▮▮▯▯▯▯▯    -19 dB
Levels and devices …
──────────────────────────────────
Devices                       ▸
Recent recordings             ▸
Open folder
──────────────────────────────────
Settings …
Quit
```

* **Pause / Resume** — stops writing without closing the file. The pause leaves no gap in the
  recording; it is noted in the log as `pause` / `resume`.
* **Level rows** — update live while the menu is open. Two moving bars mean both sides are being
  heard.
* **Levels and devices …** — one window with a live bar for **every** input and every output, each
  with a tick that decides whether it is recorded. Changes apply at once, during a recording too.
  Use it
  *before* a conversation.
* **In a virtual machine** — if the other side stays silent, Echolot tells you after 20 seconds and
  says why: audio playing on the **host** never reaches the guest. Either hold the conversation
  inside the virtual machine, or route the host output into the virtual machine's audio input
  (on Windows: *Stereo Mix* or a virtual cable, selected as the VM's sound input). See the README
  for the exact steps.

* **Devices** — by default *All available* on both sides: every input and every output monitor is
  recorded at once, so nothing has to be chosen and audio on a second output is not missed. Pick one
  device instead if you prefer; every source is offered for either side, and the change takes effect
  during a running recording.
* **Recent recordings** — the last five, each with *Play*, *Open folder*, *Open log*.
* **Quit** — quits. A running recording is closed properly first.

## 3. Where the files go

`~/Downloads/Echolot/`, two files per conversation with the same name:

```
Echolot_2026-08-12_10-15-03.opus     the audio
Echolot_2026-08-12_10-15-03.log      the speech log
```

The audio is **one mixed track**: both voices together, the way you would expect a recording to
sound. Play it in any player and just listen.

Around 30 MB per hour, so a full working day of talking costs about 240 MB.

### Who said what

That information is not lost by mixing. Echolot measures both sides *before* they are mixed, so the
`.log` next to the audio says exactly when you were talking and when the other side was:

```jsonl
{"type":"speech","src":"mic",    "start":12.40,"end":15.10,"duration":2.70,"peak_db":-18.2}
{"type":"speech","src":"speaker","start":15.62,"end":21.84,"duration":6.22,"peak_db":-22.7}
```

`src` is `mic` for you and `speaker` for the other side, and the times are seconds from the start of
the audio file - so they can be used directly as offsets when listening or transcribing. See
[TRANSCRIPT.md](TRANSCRIPT.md).

### Two separate channels instead

*Settings → Recording → Tracks → **Two separate channels*** records channel L = your microphone,
channel R = the other side. Then the voices are separable in the audio itself, which is the only way
to pull apart two people talking at the same time. The trade-off: playing such a file puts you in
one ear and the other person in the other. A normal mix can be made from it at any time:

```bash
ffmpeg -i Echolot_2026-08-12_10-15-03.opus -ac 1 gespraech.opus
```

## 4. Pre-roll — recording what happened before the click

The normal way to lose the sentence that mattered: you hear it, *then* you reach for the icon. The
pre-roll fixes that. Set it under *Settings → Pre-roll* to 1–5 minutes and Echolot keeps that
much audio in memory at all times. The double click then writes those minutes into the file as well:

```
13:02:10  the other side starts explaining
13:02:45  you double click
              ↓
the file starts at 13:00:45 (2 minutes of pre-roll) and contains the whole explanation
```

**Off by default (0 minutes).** What it costs:

| Pre-roll | Memory (mixed) | Memory (two channels) |
|---------|----------------|------------------------|
| 1 minute | 5.5 MB | 11 MB |
| 2 minutes | 11 MB | 22 MB |
| 5 minutes | 28 MB | 55 MB |

Nothing goes to disk while buffering, and unused audio is discarded — but the buffer only works if
Echolot listens continuously, so **the microphone is in use the entire time the pre-roll is on**.
That is a conscious trade-off, which is why you have to switch it on yourself.

While it is running, the tooltip and the top of the menu show how much is buffered:

```
Pre-roll: 01:23 of 2:00 buffered
```

In the recording, everything before the `recording_started` mark in the log is pre-roll:

```jsonl
{"type":"session","preroll_seconds":120.0, ...}
{"type":"speech","src":"speaker","start":18.4,"end":24.9, ...}   ← before the click
{"type":"recording_started","t":120.0}                            ← the double click
{"type":"speech","src":"mic","start":121.2,"end":123.8, ...}      ← after it
```

The buffer starts filling again by itself when a recording ends, so the next conversation has its
lead-up available too.

## 5. Before an important conversation

0. Or let it prove itself: `python3 run.py --selftest` records both sides with a test tone each
   and says plainly whether they arrived — and, separately, whether your configured devices are
   delivering anything at all.
1. Right click → **Levels and devices …**
2. Say something - the upper bar has to move.
3. Play anything, e.g. a video - the lower bar has to move.
4. Close the window, double click the icon, and talk.

If a bar stays still, pick the device explicitly under **Devices** and test again.

## 6. Keyboard shortcut

*System Settings → Keyboard → Shortcuts → Custom Shortcuts* (in German: *Systemeinstellungen →
Tastatur → Tastenkombinationen*), command:

```
python3 /home/joruf/Applications/echolot/run.py --toggle
```

Same effect as a double click, and it works even if the tray icon is not visible.

## 7. Settings

| Section | What it does |
|---------|--------------|
| **Language** | English, Deutsch, Español, Français |
| **Storage** | storage folder, autostart, number of entries under *Recent recordings* |
| **Recording** | **Tracks** (one mixed track or two separate channels), format (Opus / FLAC / WAV) and bitrate |
| **Pre-roll** | 0–5 minutes of audio kept in memory, so a recording can start in the past |
| **Devices** | fixed devices instead of automatic, follow device changes |
| **Tray and messages** | blinking on/off and its interval, which notifications appear, and after how many seconds a silent side is reported |
| **Speech detection** | thresholds for the **log** - the audio always contains everything |
| **Disk space** | warning and stop thresholds |

Everything also lives in `~/.config/echolot/settings.json` and can be edited there. Impossible
values are clamped when read, and an unreadable file falls back to the defaults.

## 8. If something is not right

| Symptom | Cause and fix |
|---------|---------------|
| No icon in the panel | The systray applet is missing. Right click the panel → *Applets* → enable the notification area applet (*Benachrichtigungsfeld*). Meanwhile `run.py --toggle` still works. |
| Other side is silent | Echolot says so by itself after 20 seconds if nothing at all arrives. First check whether the sound plays **on this computer** at all — in a virtual machine, audio from an app on the host never reaches the guest and cannot be recorded. Open *Levels and devices …* during a conversation and watch which output bar moves while you hear the person; tick exactly that device. Echolot also warns on its own when a side stayed silent for a whole recording. |
| Microphone is silent | Check the input in the Mint sound settings, then **Devices → Microphone**. |
| "Aufnahme beendet" right after starting | Disk full - Echolot stops below 300 MB free. The log's last line says `disk_full`. |
| Icon shows `!` | The tooltip and the notification say what happened; the log has the matching `source_error` entry. |
| Recording sounds out of sync | Check `gap_blocks` in the log's last line. Each gap block is 20 ms of silence that had to be filled in for that side. |
| Pre-roll shorter than expected | The buffer only holds what it has had time to collect. Right after login or after a recording it starts empty; the menu shows how much is in it. |

## 9. Note

Recording a conversation with other people generally requires their consent, and the rules differ
by country and context. Whether your use is allowed is yours to check.
