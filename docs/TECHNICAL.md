# Echolot — Technical Documentation

Architecture, the recording pipeline, the log format, and the guarantees Echolot does and does not
give.

---

## 1. Design principle

One rule decides every trade-off in this code: **keep recording**. What can be recovered later is
a wrong device label, a missing log line, a level that was 3 dB off. What cannot be recovered is
the sentence the other person just said. So a missing microphone, a dying capture process, a device
switch or an unwritable log all degrade the recording instead of ending it — and each one leaves a
trace in the log so a suspicious recording can be diagnosed afterwards.

Exactly two conditions stop a running recording, both announced and both logged:

* ffmpeg is gone — there is nothing left to write into
* the disk is below `disk.stop_mb` — writing on would damage what already exists

## 2. Module layout

```
run.py                     entry point and CLI (--toggle, --record, --devices, --check, --quit)
echolot/
  app.py                   GTK application: tray, menu, windows, signal handling
  session.py               state machine, file naming, disk guard  ← the orchestrator
  speechlog.py             voice activity detection and the JSON Lines log
  config.py                settings with clamping
  i18n.py                  translation layer, English is default and fallback
  locales/*.json           one language file per language, no code involved
  paths.py                 naming scheme, recording discovery
  instance.py              single instance lock, SIGUSR1/SIGUSR2 remote control
  autostart.py             ~/.config/autostart/echolot.desktop
  notify.py                libnotify with notify-send and stdout fallbacks
  audio/
    devices.py             pactl: sources, default sink → its monitor
    capture.py             one parec process per side, respawn, retarget
    mixer.py               the timeline: two mono sides → one mixed or split stream
    encoder.py             ffmpeg process, PCM in through stdin
    preroll.py             ring buffer of the last minutes, handed over on start
    watcher.py             pactl subscribe → device changes
  ui/
    tray.py                Gtk.StatusIcon (+ XApp fallback), double click, blinking
    menu.py                right click menu, text level meter
    level_test.py          "does it hear us both" window
    settings_window.py     settings dialog
```

Threading: capture reader threads and a supervisor per side, one mixer thread, one disk guard
thread, two watcher threads. Everything that reaches a widget goes through `GLib.idle_add`; the
recorder itself is guarded by an `RLock` so `start`/`stop` can be called from the UI thread, the
disk guard or a signal handler.

## 3. The recording pipeline

```
parec --raw -d <mic>      ─┐
  s16le, 48 kHz, mono      ├─→ mixer ──→ ffmpeg (stdin) ──→ Echolot_….opus
parec --raw -d <monitor>  ─┘   timeline      libopus
                                  │
                                  └─→ speechlog ──→ Echolot_….log
```

Two layouts, selected by `audio.layout`:

| Layout | Audio | Speaker attribution |
|--------|-------|---------------------|
| `mix` (default) | one mono track, both sides summed | from the log only |
| `split` | two channels, L = mic, R = monitor | from the log **and** from the audio |

The metrics that drive the log are taken from the per-side blocks **before** they are combined, so
the log is byte-for-byte the same work in both layouts. Mixing does not cost the transcript its
speaker labels; it only removes the ability to separate two people talking at the same instant.

Mono blocks are summed, not averaged: halving every sample would cost the whole recording 6 dB for
the sake of a few peaks. When a sum does exceed the 16-bit range it is limited and the block is
counted in `clipped_blocks`, so the distortion is visible rather than mysterious. A side that
delivered nothing needs no addition at all, which is both the fast path and the common case.

Design decisions and why:

* **Two separate `parec` processes, not one ffmpeg with two inputs.** A single ffmpeg would have to
  be restarted on every device change, tearing the file apart. Separate processes can be replaced
  one at a time.
* **Mono per side.** The sound server does the downmix — cheaper and more correct than doing it in
  Python, and it halves the data.
* **Interleaving with `array` slice assignment** (`out[0::2] = mic`), which runs in C. No numpy
  dependency, and no per-sample Python loop at 50 blocks per second.
* **`-mapping_family 255`** for Opus in the `split` layout: both channels are coded discretely
  instead of using stereo coupling, so the microphone cannot bleed into the other side's channel.
  Measured separation on the target machine: **59 dB** (channel L −90.3 dB while channel R carried a
  −31.0 dB signal). It is deliberately not set for mono, where it would mean nothing.
* **`-flush_packets 1`** plus Ogg's page structure: a file left behind by a crash or a power cut
  stays playable up to the last written page.

### The timeline

The mixer emits one 20 ms block at a time and the microphone is the clock master (it always
exists; if it is absent the speaker side takes over, and if both are gone the monotonic clock
does).

| Situation | What happens |
|-----------|--------------|
| Both sides deliver | one real block per side, paired |
| Follower side is a few blocks late | waited for, up to `FOLLOWER_GRACE_SECONDS` (100 ms) |
| One side has a backlog > 5 blocks | `_drain_backlog()` writes the pile out **in order**, pairing the starving side with silence |
| Neither side delivers for > 200 ms | `_catch_up()` fills the missing wall-clock time with silence |

The backlog drain is what makes the long grace periods safe, and it is the mechanism that protects
the other person's audio: during a microphone outage the speaker side keeps producing 50 blocks a
second, and every one of them is written. **No delivered block is ever dropped** to catch up
(`tests/test_mixer.py::test_backlog_is_written_out_in_order_without_dropping`). The only place
audio can be discarded is a capture buffer overflowing after 5 seconds of a completely stalled
mixer, and that increments `overruns` in the log.

### Known limit: skew between the sides

Filling in silence for a starving side shifts that side against the other one by 20 ms per filled
block, permanently, for the rest of the recording — in the mixed layout just as much as in the split
one, it is simply no longer separable afterwards. The log makes it measurable: `gap_blocks` counts
the filled blocks per side, so the skew is `(gap_mic − gap_speaker) × 20 ms`.

Measured on the target machine over 30 s recordings: usually **0 filled blocks**, occasionally 10
to 30 when the sound card wakes up from suspend — a skew of up to about 0.2 s per recording, mostly
acquired in the first seconds, not growing with duration. Sustained rate drift between the two
streams does not accumulate because PipeWire resamples both to the same graph clock.

Audio and log always share one time base — the block counter — so seeking to a logged timestamp
always finds the right moment in the file. A skew of a few hundred milliseconds between the two
sides is therefore immaterial for attributing utterances, and it is visible in the log when it is
not.

## 4. Pre-roll

`audio.preroll_minutes` (0 = off, up to 5) keeps the last N minutes in RAM so that a recording can
begin *before* the double click. `audio/preroll.py` runs a second, idle instance of the same pipeline:
the two capture processes feed a `Mixer` whose "encoder" is a `PrerollRing` — a bounded deque that
keeps the newest blocks and drops the oldest.

Each ring entry holds the rendered block **and that block's metrics**, because the speech log has to
stay consistent with the audio: replaying the buffer without its metrics would produce a file whose
first minutes contain speech that the log never mentions.

On start the buffer is handed over rather than restarted:

| Step | Why |
|------|-----|
| `hand_over()` stops the pre-roll mixer and takes the entries | nothing may append while they are read |
| the **capture processes are handed over alive** | recreating them would tear a hole at exactly the moment the user pressed record |
| a device that changed while buffering is retargeted, gap-free | the buffer holds the old source, the live part the new one, and the log says so |
| `Mixer(initial_blocks=N)` continues the timeline at N | log timestamps keep matching the audio |
| the buffered blocks are written on a **background thread** | see below |

Flushing is measurably slow: on the target machine 5 minutes of buffered audio take about **5
seconds** to push through libopus. Doing that on the calling thread would freeze the tray for 5
seconds and — worse — overrun the normal 5 second capture buffers, losing live audio at the very
moment the recording started. Hence two decisions: the flush runs on its own thread (`start()`
returned after 1.04 s in a measured 10 second-buffer test), and the pre-roll's capture buffers are
sized `FLUSH_HEADROOM_SECONDS = 30` instead of 5, so the live audio can queue safely while the past
is still being written.

Stopping the mixer doubles as the abort signal for a flush in progress; `stop()` waits for the flush
before closing the encoder, and the log records how much of the buffer made it (`preroll_written`
with `complete`).

Costs, and why the feature is off by default: 5.5 MB of RAM per minute (11 MB for two channels), and
capturing runs continuously, so the microphone counts as in use for as long as the pre-roll is on.
The buffer is rebuilt whenever layout, sample rate, block length or the device settings change
(`Preroll.signature()`), since the ring holds already-rendered blocks. It restarts by itself after
every recording, and while idle it follows device changes like a recording does.

## 5. Device handling

`devices.resolve()` turns the configured values into real device names:

* `mic = "auto"` → `pactl get-default-source`
* `speaker = "auto"` → the `monitor_source` of `pactl get-default-sink`, falling back to
  `<sink>.monitor` and finally to any monitor that exists

Names and states come from `pactl list short`, which is ASCII-safe. Human-readable labels come from
`pactl -f json`, which prints warnings for non-ASCII descriptions — so JSON is used for cosmetics
only and never for the decision of what to record
(`tests/test_devices.py::test_monitor_is_detected_without_json`).

`watcher.DeviceWatcher` follows `pactl subscribe`, debounces the burst of events a device change
produces, resolves again and reports a difference. `capture.CaptureProcess.retarget()` then starts
the new process **first**, waits for its first block, and only then stops the old one, so the
switch leaves no hole. If the new device never delivers, the old one keeps running and the log gets
a `retarget_failed`.

A `parec` process that dies while it should be running is respawned with a bounded backoff
(0.5 → 5 s), and the outage is bracketed in the log by `source_error` and `source_recovered` with
its measured length.

## 6. Voice activity detection

Per channel, from the same PCM blocks the encoder gets — no second capture, no extra process. The
feature is the mean absolute amplitude in dBFS, which is steadier than peak.

```
threshold = max(vad.threshold_db, noise_floor + 9 dB)
```

The noise floor is a **sliding-window minimum** (3 s, four buckets, O(1) per block). The obvious
alternative — learn the floor only during pauses — has a failure mode that matters: in a room whose
background noise sits above the fixed threshold, every block counts as speech, no pause is ever
detected, the floor is never learned, and the log degenerates into a single endless utterance. The
window minimum has no such state, because natural speech has gaps between words, so the quietest
block in any few seconds is the room rather than the voice
(`tests/test_speechlog.py::test_adaptive_threshold_follows_a_noisy_room`,
`::test_long_speech_with_word_gaps_stays_one_utterance`).

Blocks that the mixer filled in are neither speech nor a valid noise sample and are excluded from
both decisions.

Segments shorter than `min_segment_ms` (250 ms) are dropped; pauses shorter than `hangover_ms`
(400 ms) are bridged. Both channels are tracked independently, so people talking over each other
produce two overlapping segments rather than one merged blob.

## 7. Log format

JSON Lines, flushed per line. Times are seconds from the start of the audio file.

| `type` | Fields | When |
|--------|--------|------|
| `session` | app, version, started_at, audio, log, format, **layout**, **preroll_seconds**, sample_rate, block_ms, channels, devices, vad | first line |
| `speech` | src (`mic`/`speaker`), start, end, duration, peak_db | an utterance ended |
| `preroll_written` | t, blocks, of_blocks, complete | the buffer was written into the file |
| `recording_started` | t | the moment the user pressed record; everything before is pre-roll |
| `preroll_device_changed` | t, side, from, to | the device changed while the buffer was filling |
| `pause` / `resume` | t | user paused |
| `device_change` | t, side, from, to | routing switched mid-recording |
| `device_setting_changed` | t, mic, speaker | user picked another device |
| `source_error` | t, side, device, exit_code | a capture process died |
| `source_recovered` | t, side, device, outage_seconds | and came back |
| `retarget_failed` | t, side, device | the new device delivered nothing |
| `source_spawn_failed` | t, side, error | a capture process could not be started |
| `side_silent_at_start` | t, side, device | a side produced nothing at startup |
| `side_unavailable` | t, side | no device for this side at all |
| `device_problem` | t, message | resolution had to fall back |
| `disk_warning` / `disk_full` | t, free_mb | space is running out |
| `session_end` | ended_at, duration, blocks, gap_blocks, silence_filled_blocks, **clipped_blocks**, captured_blocks, restarts, overruns, speech_seconds, segments, audio_bytes, encoder_returncode | last line |

`speech` lines are written when a segment **ends**, so lines are not globally sorted by `start`; a
consumer sorts by it. Everything needed to judge a recording's integrity is in `session_end`.

See [TRANSCRIPT.md](TRANSCRIPT.md) for turning this into a transcript.

## 8. Guards

| Guard | Behaviour |
|-------|-----------|
| Disk space | checked every `disk.check_interval_s` (15 s): one warning below `warn_mb` (1 GB), clean stop below `stop_mb` (300 MB) |
| Broken settings file | falls back to defaults, reports it, tray still comes up |
| Impossible setting values | clamped on load (`config.validate`) |
| Settings file from an older version | missing keys are filled in and written back once (`config.needs_migration`) |
| Unknown language code | ignored, the previous language stays active |
| Language file with gaps | the missing keys answer in English |
| Second start | signals the running instance instead of opening a second icon (`flock` + PID) |
| Unwritable log | the audio keeps recording; `SpeechLog.write_error` records why |
| A side silent for a whole recording | warned about when the recording ends (`SILENT_SIDE_MIN_SECONDS`), because a routing problem found days later is a lost conversation |
| No systray | falls back to `XApp.StatusIcon`, then notifies that `--toggle` still works |

## 9. Languages

`i18n.py` plus one JSON file per language in `echolot/locales`. English is both the default and the
fallback, so a partial translation is usable rather than broken. Keys are dotted and namespaced by
where they appear (`menu.`, `session.`, `cli.`, …); values carry `{named}` placeholders and are
filled with `str.format`, which means a malformed call renders the raw template instead of raising
in the middle of a notification.

No relabelling machinery is needed anywhere, and that is a consequence of the existing design: the
menu is rebuilt on every right click, the tooltip on every tick, and the windows are built when they
open. Switching the language therefore takes effect without touching a single widget - only windows
that are already open keep their old labels.

Two things deliberately do **not** follow the language:

* **the speech log** - `mic`, `speaker`, `speech`, `session_end` stay English keys, so a transcript
  script works on a recording no matter who made it
* **device names** - they come from the sound server, not from us

Values that look like text but are formatting: `common.decimal_separator` is what `paths.human_size`
uses, so a size reads `2.0 KB` in English and `2,0 KB` in the other three.

The `.desktop` entries carry *all* languages at once (`Comment[de]=`, …), because the desktop reads
them in the desktop's language, not in ours. They are generated by `python3 -m echolot.autostart`,
which `install.sh` calls - the installer cannot drift away from the app that way.

Adding a language is a file, not a change: copy `en.json`, translate the values, set `_label`, save
as `<code>.json`. `tests/test_i18n.py` then holds it to the same standard as the others - no missing
keys, no renamed placeholders, no unbalanced markup, and no keys the code never asks for.

## 10. Tray backend

The double click requirement dictates the choice. AppIndicator — the usual modern answer — reports
no clicks at all, only a menu, so it is unusable here. `Gtk.StatusIcon` does report button events
and is what the Cinnamon systray applet carries (verified on the target machine:
`is_embedded == True`, 24 px). `XApp.StatusIcon` is the fallback if nothing embeds the icon within
3 seconds.

Double clicks are detected from single click events in both backends, using the desktop's own
`gtk-double-click-time` (400 ms). GTK also emits `_2BUTTON_PRESS`, but acting on that *and* on the
plain presses would toggle twice — so only plain presses feed the detector.

## 11. Tests

```bash
python3 -m pytest          # 162 tests; two need a working sound server, the rest do not
```

Covered: naming scheme and pairing, settings clamping and migration, device resolution including the
JSON-unavailable and no-monitor cases, both layouts (mono summing, limiting, lone-side pass-through,
split channel order), backlog draining without loss in either layout, encoder command lines and real
mono/stereo files through ffmpeg, the pre-roll ring (pairing contract, window eviction, handover,
timeline continuation, rebuild-on-settings-change, and two tests that really capture from the sound
server), VAD behaviour including the noisy-room regression, log schema, disk guard warning and
stopping, a signal-killed encoder being reported as a warning rather than a failure, capture
buffering and overrun counting, autostart entry, double click detection, and a GUI smoke test that
builds every window and the menu against the real GTK.

### Manual verification

Things that need real audio and a real panel:

1. `python3 run.py` — icon appears in the panel
2. Double click — icon blinks, notification appears, two files show up in `~/Downloads/Echolot/`
3. Speak into the microphone and play a video, then double click again
4. `ffprobe` reports Opus with 1 channel (or 2 in the `split` layout, you left, the other side right)
5. Both voices are audible in the mix; in `split`,
   `ffmpeg -i <file> -af "pan=mono|c0=c0,volumedetect" -f null -` per channel shows signal on both
6. The `.log` contains `speech` entries for both `src` values with plausible times — this is what
   proves the mix did not cost the speaker attribution
7. Switch the output device in the Mint sound settings while recording — `device_change` appears in
   the log and the speaker channel stays audible
8. `pkill -f "parec --raw -d alsa_input"` while recording — `source_error` and `source_recovered`
   appear, the recording continues, the other channel is unaffected
9. Log out and back in — the icon is there again and is not blinking
10. Pre-roll: set *Pre-roll* to 1 minute, wait a minute (the menu shows the buffer filling), play
    something, wait for it to end, *then* double click. The played audio has to be in the file
    before the `recording_started` mark in the log — that is the whole point of the feature.
11. With the pre-roll on, stop a recording and check that the buffer starts filling again by itself
