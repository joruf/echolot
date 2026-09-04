# Echolot

**Echolot records conversations from the Linux system tray. Double click the icon and it records** —
your microphone and whatever the other side says through your speakers, mixed into one recording.
Next to the audio it writes a log of who spoke when, so a transcript can be generated from it later.

Built for Linux Mint / Cinnamon on PipeWire or PulseAudio. Runs on the system Python with the GTK
bindings the desktop already ships: no virtual environment, no downloads, no build step.

Interface available in **English** (default), **German**, **Spanish** and **French**.

**Deeper documentation:** [User Manual](docs/MANUAL.md) ·
[Technical Documentation](docs/TECHNICAL.md) · [Building a Transcript](docs/TRANSCRIPT.md)

---

## Contents

1. [What it does](#1-what-it-does)
2. [Requirements and installation](#2-requirements-and-installation)
3. [Using it](#3-using-it)
4. [What ends up on disk](#4-what-ends-up-on-disk)
5. [Proving that both sides are recorded](#5-proving-that-both-sides-are-recorded)
6. [Track layout: mixed or split](#6-track-layout-mixed-or-split)
7. [Pre-roll: recording the past](#7-pre-roll-recording-the-past)
8. [Languages](#8-languages)
9. [Settings reference](#9-settings-reference)
10. [Command line reference](#10-command-line-reference)
11. [The speech log](#11-the-speech-log)
12. [Behaviour when things go wrong](#12-behaviour-when-things-go-wrong)
13. [How it works](#13-how-it-works)
14. [Project layout](#14-project-layout)
15. [Tests](#15-tests)
16. [Troubleshooting](#16-troubleshooting)
17. [Known limits](#17-known-limits)
18. [Legal note](#18-legal-note)

---

## 1. What it does

| | |
|---|---|
| **Both sides in one file** | your microphone and the system output (the other person), recorded together |
| **Everything by default** | every input and every output monitor at once, so no device choice can be wrong and audio on a secondary output is not missed |
| **Double click to record** | one gesture in the tray starts and stops it; a single click deliberately does nothing |
| **Blinking icon** | while a recording runs the icon blinks red — it is never unclear whether you are recording |
| **Who spoke when** | measured per side *before* mixing and written to a JSON Lines log, so a transcript can attribute speakers without a diarisation model |
| **Pre-roll** | optionally keeps the last 1–5 minutes in RAM, so a recording can begin *before* you pressed anything |
| **Survives trouble** | device switches, dying capture processes and missing microphones degrade the recording instead of ending it, and every incident lands in the log |
| **Starts with the session** | autostart entry, single instance, remote control by signal for a keyboard shortcut |
| **Four languages** | English, German, Spanish, French — the whole interface plus the command line output |

Not in scope: editing, uploading, cloud anything, video, and speech recognition itself — see
[docs/TRANSCRIPT.md](docs/TRANSCRIPT.md) for how to feed a recording to Whisper.

## 2. Requirements and installation

```bash
cd ~/Applications/echolot
chmod +x install.sh
./install.sh
```

The installer checks the requirements, offers to install what is missing, writes the autostart entry
and adds a launcher to the application menu. It generates both `.desktop` files by calling the app
itself, so installer and app cannot drift apart.

| Package | Why |
|---------|-----|
| `python3` ≥ 3.10 | the app; developed against 3.12 |
| `python3-gi`, `gir1.2-gtk-3.0` | tray icon and windows |
| `ffmpeg` | encodes the recording |
| `pulseaudio-utils` | `parec` captures, `pactl` finds the devices |

All four are present on a standard Linux Mint install. Manually, if needed:

```bash
sudo apt install python3-gi gir1.2-gtk-3.0 ffmpeg pulseaudio-utils
```

Start it, or log out and back in:

```bash
python3 run.py
```

Check that everything is in place at any time:

```bash
python3 run.py --check
```

## 3. Using it

| Action | Result |
|--------|--------|
| **Double click** the icon | starts the recording, and stops it again |
| **Right click** | the menu (below) |
| Icon **blinks red** | a recording is running |
| Icon **amber, pause bars** | paused |
| Icon **red with `!`** | something is wrong — the tooltip and the notification say what |
| Hover the icon | elapsed time, both levels, devices in use, file name, free space |

The menu is rebuilt every time it opens, so it always shows the truth:

```
Echolot – Recording · 12:34
──────────────────────────────────
Stop recording
Pause
──────────────────────────────────
You         ▮▮▮▮▮▯▯▯▯▯▯▯    -28 dB     ← live while the menu is open
Other side  ▮▮▮▮▮▮▮▯▯▯▯▯    -19 dB
Levels and devices …
──────────────────────────────────
Devices                       ▸       ← all sources (default), automatic, or one
Recent recordings             ▸       ← last five: play, open folder, open log
Open folder
──────────────────────────────────
Settings …
Quit
```

**Before an important conversation**, use **Levels and devices …**: the bar of the device you speak
into has to move, and so does the bar of the output you hear the other person through. One when you
speak, one when you play something. That is the one check that catches a dead side *before* it costs
you the conversation rather than after.

**Pause** stops writing without closing the file and leaves no gap in the recording; it is noted in
the log as `pause` / `resume`.

## 4. What ends up on disk

Every conversation produces two files in `~/Downloads/Echolot/` that share one basename:

```
Echolot_2026-08-12_10-15-03.opus     the audio
Echolot_2026-08-12_10-15-03.log      who spoke when, JSON Lines
```

The shared name is what lets any script pair them up without an index or a database. A recording
started in the same second as an existing one becomes `…_2`.

| Format | Size per hour | When |
|--------|---------------|------|
| **Opus** (default, 64 kbit/s) | ≈ 30 MB | speech; a full working day of talking costs ≈ 240 MB |
| FLAC | ≈ 350 MB | lossless, if a recording has to be bit-exact |
| WAV | ≈ 660 MB | maximum compatibility with old tools |

### Recording everything

Both sides default to `all`: the microphone side is **every input**, the other side is **every output
monitor** the machine has. The devices of a side are summed, and an idle device delivers digital
silence, so having it along costs nothing. Two consequences:

* No device choice can be wrong, and audio playing on a *secondary* output is captured rather than
  missed. Verified by playing a tone only into a second, non-default output: it is in the recording.
* A device that appears during a conversation joins its side without interrupting anything.

`auto` (follow the system default) and naming one device by hand remain available per side, in
*Devices* and in the settings file. Every source is offered for **either** side, because the other
side's audio does not always arrive on a monitor — a virtual cable or a line input is a real case.

### Recording inside a virtual machine

In a guest the sound card is virtual: it carries what the guest itself plays and nothing from the
host. A conversation held in a program on the **host** therefore cannot be recorded from inside the
guest — the output monitor delivers exact digital silence, and no setting inside the guest changes
that. Echolot detects the guest (`systemd-detect-virt`, DMI as a fallback) and says so **after 20
seconds**, with the two ways out, instead of leaving it to be discovered when the conversation is
over.

* **Hold the conversation inside the guest** — Teams, Meet or Zoom in a browser here. Nothing to
  configure; both sides are captured as usual.
* **Route the host output into the guest's audio input.** On a Windows host: enable *Stereo Mix* in
  the recording devices (or install a virtual audio cable), then point the virtual machine's sound
  card at it (VMware: *Settings → Sound Card → specify host sound card*). The host mix arrives on the
  guest's **input**, which the default *record everything* captures without any further setting. To
  keep your own voice as well, mix the microphone into that device on the host — or pass a USB
  microphone through to the guest, so the two sides stay on separate devices and therefore on
  separate channels.

### Levels and devices

*Levels and devices …* in the tray menu opens one window that answers both questions at once, because
they belong together: **what is arriving on each device right now**, and **which devices go into the
recording**.

```
Inputs   (microphones and line inputs - normally your side, channel L)
  [x] Use all available automatically
  [x] Analog Stereo Microphone      ▓▓▓▓▓▓▁▁▁▁▁▁   -23 dB
  [ ] USB Headset                   ▁▁▁▁▁▁▁▁▁▁▁▁     -- dB

Outputs  (what is being played - normally the other side, channel R)
  [ ] Use all available automatically
  [x] Monitor of Analog Stereo Out  ▓▓▓▓▁▁▁▁▁▁▁▁   -34 dB
  [ ] Monitor of EcholotProbe       ▁▁▁▁▁▁▁▁▁▁▁▁     -- dB
```

* **Every listed device is metered live**, each from its own capture — so the bar tells you which
  device actually carries the other side. Watching the bar move is the only reliable way to find that
  out, which is why the tick sits right next to it.
* **A tick decides what is recorded.** Changes apply immediately, during a running recording too.
* **Use all available automatically** is the default per side (`all`): every device of that kind,
  including ones that appear later. Turning it off freezes exactly what is on screen into an explicit
  list, so nothing changes under you at the moment you take manual control.
* Unticking everything on a side is allowed and says so plainly — that side then is not recorded.
* *Rescan devices* re-reads the device list without closing the window.

## 5. Proving that both sides are recorded

`--selftest` records for real and then looks inside the file. It answers two
questions separately, because they fail for completely different reasons:

```
$ python3 run.py --selftest
Echolot self test

1) Recording chain - virtual devices, one test tone each, separate channels
   OK   You: own tone -6.2 dBFS, separation 78 dB
   OK   Other side: own tone -6.2 dBFS, separation 77 dB
   -> Both sides are being recorded.

2) The devices you have set - listening for 2 seconds
   signal   You: alsa_input.pci-0000_02_02.0.analog-stereo (-78.3 dBFS)
   SILENT   Other side: alsa_output...analog-stereo.monitor - exact digital zero, nothing arrives here

Result: Echolot works, but nothing arrives from Other side. That sound does not reach this machine.
```

**Phase 1** creates two virtual devices, plays **440 Hz** into the microphone side and **880 Hz** into
the other side, records them through the whole pipeline, and then measures each channel for *its own*
frequency and *the other's*. That is what makes the verdict worth anything: a check for "is there
sound" passes with the sides swapped or with one side bleeding into both channels — asking for a
specific frequency in a specific channel cannot.

**Phase 2** listens to the devices that are actually configured and distinguishes a **quiet room**
(a noise floor, -78 dBFS above) from **exact digital zero**, which means nothing is connected to that
side at all. In a virtual machine that is the normal state for the output side, and it is what a green
phase 1 with a red phase 2 tells you: Echolot is fine, the sound is not here.

Exit codes: `0` all good, `1` the recording chain failed, `2` the chain works but a configured device
delivers nothing.

The same machinery runs in the test suite (`tests/test_end_to_end.py`), for **every** output format —
opus is decoded back before analysis, so the format people actually record in is proven rather than
assumed:

| Format | Microphone side | Other side | Separation |
|---|---|---|---|
| opus (default) | -6.2 dBFS | -6.2 dBFS | 75-87 dB |
| flac | -6.2 dBFS | -6.2 dBFS | 78-96 dB |
| wav | -6.1 dBFS | -6.2 dBFS | 76-116 dB |

## 6. Track layout: mixed or split

*Settings → Recording → Tracks*

| Layout | Audio | Speaker attribution |
|--------|-------|---------------------|
| **One mixed track** (default) | mono, both voices together — sounds like a normal recording | from the log |
| **Two separate channels** | channel L = your microphone, channel R = the other side | from the log **and** from the audio |

Mixing does **not** cost you the speaker attribution: Echolot measures both sides before they are
combined, so the log says exactly when each side was talking either way. What only the split layout
can do is pull apart two people talking at the *same instant*.

Measured channel separation in the split layout: **59 dB** (channel L at −90.3 dB while channel R
carried a −31.0 dB signal), because Opus is told to code the channels discretely
(`-mapping_family 255`) instead of using stereo coupling.

A normal mono mix can be made from a split file at any time:

```bash
ffmpeg -i Echolot_2026-08-12_10-15-03.opus -ac 1 conversation.opus
```

## 7. Pre-roll: recording the past

*Settings → Pre-roll* — **off by default**

The usual way to lose the sentence that mattered is to hear it first and reach for the icon second.
With a pre-roll of N minutes, Echolot keeps that much audio in RAM at all times and the double click
writes it into the file as well:

```
13:02:10  the other side starts explaining
13:02:45  you double click
              ↓
the file begins at 13:00:45 and contains the whole explanation
```

| Pre-roll | RAM (mixed) | RAM (split) |
|----------|-------------|-------------|
| 1 min | 5.5 MB | 11 MB |
| 2 min | 11 MB | 22 MB |
| 5 min | 28 MB | 55 MB |

Nothing is written to disk while buffering and unused audio is discarded — but the buffer only exists
if Echolot listens continuously, so **the microphone counts as in use the whole time the pre-roll is
on**. That is why you have to switch it on yourself.

While it runs, the menu header and the tooltip show `Pre-roll: 01:23 of 2:00 buffered`. The buffer
starts filling again by itself after every recording, and it follows device changes while idle.

In the log, `preroll_seconds` says how much lead-up a file contains and `recording_started` marks the
moment record was actually pressed.

## 8. Languages

| Code | Language | File |
|------|----------|------|
| `en` | English (default) | `echolot/locales/en.json` |
| `de` | Deutsch | `echolot/locales/de.json` |
| `es` | Español | `echolot/locales/es.json` |
| `fr` | Français | `echolot/locales/fr.json` |

Menu, tooltip, notifications, both windows and the command line output all follow. Switch under
*Settings → Language* or set `"language": "de"` in the settings file. The menu and the tooltip follow
immediately; windows that are already open keep the language they were opened with.

Two things deliberately do **not** follow the language:

* **the log** keeps English keys (`mic`, `speaker`, `speech`), so a transcript script works on any
  recording no matter who made it
* **device names** come from the sound server, not from Echolot

**Adding a language needs no code change:** copy `en.json`, translate the values, keep the keys and
the `{placeholders}`, set `_label` to the language's own name (that is what the dropdown shows), and
save it as `<code>.json`. Missing keys fall back to English, so a partial file is already usable.
`python3 -m pytest tests/test_i18n.py` then holds the new file to the same standard as the others.

## 9. Settings reference

*Settings …* in the menu, or edit `~/.config/echolot/settings.json` directly. Impossible values are
clamped when the file is read, an unreadable file falls back to the defaults, and settings added by a
later version are filled in and written back once — the tray always comes up.

| Key | Default | Meaning |
|-----|---------|---------|
| `language` | `"en"` | interface language: `en`, `de`, `es`, `fr` |
| `recordings_dir` | `null` | where recordings go; `null` means `~/Downloads/Echolot` |
| `recent_limit` | `5` | entries under *Recent recordings* |
| `autostart` | `true` | write `~/.config/autostart/echolot.desktop` |
| `audio.layout` | `"mix"` | `mix` = one mono track, `split` = two channels |
| `audio.format` | `"opus"` | `opus`, `flac`, `wav` |
| `audio.bitrate_kbps` | `64` | Opus only |
| `audio.preroll_minutes` | `0` | 0 = off, up to 5 |
| `audio.sample_rate` | `48000` | 8000 / 12000 / 16000 / 24000 / 48000 |
| `audio.block_ms` | `20` | pipeline block length; also the log's time resolution |
| `devices.mic` | `"all"` | `all` = every input, `auto` = the system default, or one source name |
| `devices.speaker` | `"all"` | `all` = every output monitor, `auto` = monitor of the default sink, or one source name |
| `devices.follow_default` | `true` | take in devices that appear later, during a recording too |
| `tray.blink` | `true` | icon blinks while recording |
| `tray.blink_interval_ms` | `700` | 200–5000 |
| `warnings.silent_side_seconds` | `20` | say so **during** the conversation when a side delivers nothing but exact zeros; 0 = off |
| `notifications.on_start` | `true` | message when a recording starts |
| `notifications.on_stop` | `true` | message when it ends |
| `notifications.on_error` | `true` | messages about problems |
| `vad.threshold_db` | `-45.0` | speech threshold for the **log** — the audio always has everything |
| `vad.min_segment_ms` | `250` | shorter utterances are not logged |
| `vad.hangover_ms` | `400` | shorter pauses are bridged into one utterance |
| `vad.adaptive_noise_floor` | `true` | raise the threshold with the room's background noise |
| `disk.warn_mb` | `1024` | warn once below this much free space |
| `disk.stop_mb` | `300` | stop the recording cleanly below this |
| `disk.check_interval_s` | `15` | how often free space is checked |

## 10. Command line reference

```bash
python3 run.py                 # start the tray icon
python3 run.py --autostart     # same, quiet: no "is running" notification
python3 run.py --toggle        # start/stop recording in the running instance
python3 run.py --quit          # stop the running instance
python3 run.py --record 60     # record 60 seconds without any GUI (0 = until Ctrl-C)
python3 run.py --devices       # which devices would be recorded, and all alternatives
python3 run.py --check         # requirements, storage, free space, layout, pre-roll
python3 run.py --selftest      # record both sides for real and prove it (exit 0 / 1 / 2)
python3 run.py --version
```

A second start does not open a second icon: it signals the running one and exits. That makes
`--toggle` the right thing to bind to a keyboard shortcut, under *System Settings → Keyboard →
Shortcuts*:

```
python3 /home/joruf/Applications/echolot/run.py --toggle
```

If no instance is running, `--toggle` starts one and begins recording immediately.

## 11. The speech log

JSON Lines, one object per line, flushed immediately — a log that only survived a clean shutdown
would be worthless for exactly the sessions you care about. All times are seconds from the start of
the audio file, so they work directly as playback offsets.

```jsonl
{"type":"session","app":"Echolot","layout":"mix","preroll_seconds":0.0, ...}
{"type":"speech","src":"mic","start":12.40,"end":15.10,"duration":2.70,"peak_db":-18.2}
{"type":"speech","src":"speaker","start":15.62,"end":21.84,"duration":6.22,"peak_db":-22.7}
{"type":"session_end","duration":1843.2,"speech_seconds":{"mic":412.6,"speaker":905.1}, ...}
```

| `type` | Meaning |
|--------|---------|
| `session` | header: version, start time, files, format, layout, `preroll_seconds`, devices, VAD settings |
| `speech` | one utterance: `src` (`mic`/`speaker`), `start`, `end`, `duration`, `peak_db` |
| `preroll_written` | the buffered lead-up was written, and whether it was `complete` |
| `recording_started` | the moment record was pressed; everything before it is pre-roll |
| `side_no_audio` | a side has been delivering nothing but exact zeros - reported while the recording runs |
| `source_added` | a device appeared and joined its side while recording |
| `preroll_device_changed` | the device changed while the buffer was filling |
| `pause` / `resume` | user paused |
| `device_change` | routing switched mid-recording |
| `device_setting_changed` | user picked another device |
| `source_error` / `source_recovered` | a capture process died / came back, with the outage length |
| `retarget_failed` | a new device delivered nothing, the old one kept running |
| `source_spawn_failed` | a capture process could not be started at all |
| `side_silent_at_start` | a side produced nothing when the recording started |
| `side_unavailable` | no device for that side at all |
| `device_problem` | device resolution had to fall back |
| `disk_warning` / `disk_full` | space running out / exhausted |
| `session_end` | totals: duration, blocks, `gap_blocks`, `clipped_blocks`, `captured_blocks`, restarts, overruns, `speech_seconds`, segments, audio bytes, ffmpeg exit code |

`speech` lines are written when a segment *ends*, so lines are not globally sorted by `start` — sort
by it. The last line answers whether a recording can be trusted: `speech_seconds` of `0.0` for one
side means that side was silent, and `gap_blocks` counts 20 ms blocks that had to be filled in.

## 12. Behaviour when things go wrong

The recording keeps running. That is the design principle: what can be recovered later is a wrong
label or a missing log line — what cannot is the sentence the other person just said.

* **Headset plugged in mid-conversation** — the new device is picked up without a gap (the new capture
  process has to deliver before the old one is stopped), and the switch is logged as `device_change`.
* **A capture process dies** — restarted automatically with a bounded backoff; the outage and its
  measured length appear as `source_error` / `source_recovered`, and the other side keeps recording
  throughout. Verified by killing `parec` mid-recording: the other channel was uninterrupted.
* **A side delivers nothing** — silence is written for it so both sides stay aligned, and it is
  counted in the log instead of quietly disappearing.
* **Audio plays on a secondary output** — captured anyway: every output monitor is recorded, not just
  the default one. A device that appears mid-recording joins its side (`source_added`).
* **A side stays digitally silent** — said after 20 seconds, in a notification that stays on screen
  and in the tray tooltip. Inside a virtual machine the message names the cause and the fix.
* **No microphone at all** — the other side is still recorded, and you get a warning.
* **The log cannot be written** — the audio keeps recording.
* **ffmpeg is killed with the session** (logging out) — reported as a warning with duration and size,
  not as a failure: the file is playable up to the last written page.
* **No systray in the panel** — falls back to `XApp.StatusIcon`, then tells you that `--toggle` still
  works.
* **A side delivers nothing but digital silence** — announced after 20 seconds, during the
  conversation, because that is when it can still be fixed. Logged as `side_no_audio`.

Only two conditions stop a running recording, both announced and both logged:

| Condition | Why |
|-----------|-----|
| ffmpeg is gone | there is nothing left to write into |
| free space below `disk.stop_mb` | writing on would damage what already exists |

## 13. How it works

```
parec --raw -d <mic>      ─┐
  s16le, 48 kHz, mono      ├─→ mixer ──→ ffmpeg (stdin) ──→ Echolot_….opus
parec --raw -d <monitor>  ─┘   timeline      libopus
                                  │
                                  └─→ speechlog ──→ Echolot_….log
```

* **Two separate `parec` processes**, not one ffmpeg with two inputs: a single ffmpeg would have to be
  restarted on every device change, tearing the file apart.
* **Mono per side** — the sound server does the downmix, which is cheaper and more correct than doing
  it in Python, and halves the data.
* **The mixer owns the timeline** and never blocks on a side. A side that is late is waited for; a
  backlog is written out **in order and never dropped**; time in which neither side produced anything
  is filled with silence rather than being swallowed.
* **The pre-roll** is the same pipeline running into a ring buffer while idle. On start the buffer and
  the still-running capture processes are handed over, and the buffered audio is pushed into the
  encoder on a background thread — measured at 5.2 s for a 5 minute buffer, which is why it must not
  happen on the UI thread.
* **`Gtk.StatusIcon`** is the tray backend because it is the only one that reports clicks;
  AppIndicator offers a menu only, which cannot satisfy a double click.

Full detail, including the measurements behind these choices, in
[docs/TECHNICAL.md](docs/TECHNICAL.md).

## 14. Project layout

```
run.py                     entry point and CLI
install.sh                 requirement check, autostart entry, menu launcher
echolot/
  app.py                   GTK application: tray, menu, windows, signals
  session.py               state machine, file naming, guards  ← the orchestrator
  speechlog.py             voice activity detection and the JSON Lines log
  config.py                settings, clamping, migration
  i18n.py                  translation layer
  locales/{en,de,es,fr}.json
  paths.py                 naming scheme, recording discovery
  instance.py              single instance lock, signal remote control
  autostart.py             .desktop entries in every language
  notify.py                libnotify → notify-send → stdout
  audio/
    devices.py             pactl: sources, default sink → its monitor
    capture.py             one parec process per side, respawn, gap-free retarget
    mixer.py               timeline, mixing, limiting, backlog drain
    encoder.py             ffmpeg process fed through stdin
    preroll.py             ring buffer of the last minutes
    watcher.py             pactl subscribe → device changes
  ui/
    tray.py                icon, double click detection, blinking
    menu.py                right click menu, text level meter
    levels_window.py       live level and a tick per device
    settings_window.py     settings dialog
resources/                 icons and the .desktop template
docs/                      MANUAL, TECHNICAL, TRANSCRIPT
tests/                     249 tests
```

## 15. Tests

```bash
python3 -m pytest
```

249 tests. The end-to-end recordings and a few device tests need a working sound server and skip
themselves without one; the GUI smoke tests skip themselves without a display. Everything else runs
anywhere.

Covered: the naming scheme and file pairing, settings clamping and migration, device resolution
including the JSON-unavailable and no-monitor cases, both track layouts (mono summing, limiting,
lone-side pass-through, split channel order), the promise that no delivered audio is ever dropped,
the pre-roll ring and its handover, encoder command lines plus real mono and stereo files through
ffmpeg, voice activity detection including the noisy-room case, the log schema, the disk guard,
capture buffering and overrun counting, all four language files for completeness and matching
placeholders, the autostart entry, double click detection, and a GUI smoke test that builds every
window and the menu against the real GTK.

On top of that, `tests/test_end_to_end.py` records **for real**: virtual devices, 440 Hz into the
microphone side and 880 Hz into the other side, through the whole pipeline, in **every** output format
(opus decoded back before analysis). Each channel is then measured for its own frequency and the
other's, which is what catches swapped sides and bleed - a plain "is there sound" check cannot. The
analysis itself is tested separately in `tests/test_selftest.py`, because a wrong analysis would make
a green test meaningless.

Everything that needs real audio and a real panel is a manual checklist in
[docs/TECHNICAL.md](docs/TECHNICAL.md#manual-verification).

## 16. Troubleshooting

| Symptom | Cause and fix |
|---------|---------------|
| No icon in the panel | The systray applet is missing: right click the panel → *Applets* → enable the notification area applet. `run.py --toggle` works meanwhile. |
| The other side is silent | First: is the audio playing **on this computer** at all? See below — in a virtual machine it is not. If it is, open *Levels and devices …* during the conversation and watch which output bar moves; tick that one. The log confirms either case: `speech_seconds` is `0.0` for that side, and Echolot warns after 20 seconds. |
| The microphone is silent | Check the input in the Mint sound settings, then *Devices → Microphone*. |
| "Recording finished" right after starting | Disk full; Echolot stops below `disk.stop_mb`. The log's last line says `disk_full`. |
| Icon shows `!` | The tooltip and the notification say what happened, and the log has the matching `source_error`. |
| Recording sounds out of sync | Check `gap_blocks` in the log's last line: each one is 20 ms of silence that had to be filled in for that side. |
| Pre-roll shorter than expected | The buffer only holds what it has had time to collect; after login or after a recording it starts empty. The menu shows how much is in it. |
| Interface in the wrong language | *Settings → Language*, or `"language"` in the settings file. |

### Echolot can only record what this computer plays

The other side is captured from the **monitor of this machine's output** — a tap on what the sound
server here is playing. Anything that never passes through it cannot be recorded, no matter how
Echolot is configured:

* **In a virtual machine**, only what plays *inside* the VM. A meeting app running on the host, or a
  headset attached to the host, never reaches the guest's sound card. The microphone can still work
  (the host forwards it), which makes the recording look half-broken: your voice is there, the other
  side is not.
* **A phone call on an actual telephone**, unless it is on speaker in front of the microphone.
* **A second computer**, obviously.

You do not have to notice this yourself. A source that exists always carries a noise floor, so
samples that are **all exactly zero** mean there is no audio stream at all — which is different from
nobody talking, and is what makes an early warning possible without false alarms. After
`warnings.silent_side_seconds` (20 s by default) of that, Echolot says so **while the conversation is
still running**, which is the only moment the routing can still be fixed:

> Nothing at all is arriving from Other side - complete silence for 00:20. If you can hear the other
> person, their sound is not reaching this computer.

Two more ways to tell:

1. During a conversation, open *Levels and devices …*. Some output bar has to move while you hear the other
   person. If it stays flat, the audio is not on this machine.
2. Afterwards, the log's last line shows `"speech_seconds": {"mic": 179.7, "speaker": 0.0}`, and
   Echolot warns about a side that stayed silent for a whole recording if it had not said so
   already.

The fix is to move the sound onto this machine — run the call in a browser or app **here** — because
no setting can capture audio that is not present.

## 17. Known limits

* **Skew between the sides.** Filling silence for a starving side shifts that side against the other
  by 20 ms per filled block, permanently. Measured over 30 s recordings: usually 0 filled blocks,
  occasionally 10–30 when the sound card wakes from suspend — up to about 0.2 s per recording,
  acquired in the first seconds and not growing with duration. Always visible in `gap_blocks`.
* **Simultaneous speech in the mixed layout** cannot be separated afterwards. Use the split layout if
  that matters for your material.
* **Pre-roll costs an open microphone.** There is no way to have the last two minutes without
  listening for those two minutes.
* **X11 / Cinnamon** is what this is tested on. The tray backend cascade covers other panels, but
  Wayland-only sessions without a legacy systray are untested.
* **No speech recognition included** — Echolot produces the material and the timing; the transcript is
  one script away, see [docs/TRANSCRIPT.md](docs/TRANSCRIPT.md).

## 18. Legal note

Recording a conversation with other people generally requires their consent, and the rules differ by
country and context. Whether your use is allowed is yours to check.
