# Bifrost on Windows: handoff

**Status: nothing Windows-specific has been written yet.** Bifrost runs on Linux only. This
document is everything learned while scoping the port, so a session on a Windows machine
can start straight away instead of rediscovering it. The work was deliberately postponed
until it can be built and tested on a real Windows PC (see "What cannot be verified
without one").

## Start here on the Windows machine

1. Clone the public repo: `git clone https://github.com/hstandish315/valhalla-tools` and work in `bifrost/`.
2. Install Python 3.12+, then `python -m venv venv` and `venv\Scripts\pip install numpy`.
3. Install ffmpeg and make sure `ffmpeg.exe` and `ffprobe.exe` are on `PATH`.
4. Run `venv\Scripts\python -m unittest discover -s tests`. **Expect failures** from tests that use
   Linux tools (`sh -c`, PipeWire, `pgrep`) and from anything importing `fcntl`. Those are the
   seams to fix; the rest of the suite is the portable core and should pass or show small,
   specific Windows differences (paths, line endings, `os.sync`).
5. Read the sections below, then ask the user the three questions under "Decisions still open".

## What is portable and what is not

Measured with a module audit: about **20%** of the application code (1,226 of 6,099 lines,
excluding tests) is portable as it stands.

| Portable now (pure Python, numpy, ffmpeg) | Linux-tied |
|---|---|
| `dsp.py` (sweep, pulse, limiter, chain), `presets.py`, `session.py` | `engine.py`: output through `pw-play` |
| `library.py`, `settings.py` (default paths need a Windows rule) | `live.py`, `ui/live_view.py`: PipeWire virtual sink, `pw-dump`, `pw-metadata`, `pw-record` |
| `sources/*` (Openverse, Internet Archive, matching) | `cd.py`, `cdcli.py`, `ui/rip_view.py`: `fcntl` ioctls, `/dev/sr*`, `/sys/block`, `gst-launch cdparanoiasrc`, `eject` |
| `musicbrainz.py` (lookup, cover art) | `export.py`: `lsblk`, `udisksctl`, `os.sync` (not on Windows) |
| the portable halves of `cd.py` and `export.py` (below) | `app.py`, `theme.py`, `ui/*`: GTK4 (portable in principle); fonts "Ubuntu Sans/Mono" |

Inside the Linux-tied files a good deal is portable and should stay where it is: TOC maths
and `disc_id`, tagging, `embed_cover`, `rip_track`'s orchestration and verification
(`cd.py`); `copy_tracks`, names, attribution, the processing and conversion transforms
(`export.py`); ffmpeg decoding (`engine.py`).

Small portability problems already known:
- `cd.py` imports `fcntl` at the top, which fails on Windows. Import it lazily inside the Linux backend.
- `export.safely_remove` calls `os.sync()`, which does not exist on Windows.
- Subprocess calls use the literal names `"ffmpeg"` and `"ffprobe"`; a Windows build should resolve them once (PATH, or a bundled folder) and pass `CREATE_NO_WINDOW`, otherwise a console window flashes for every call.
- Default data folders are under `~/.local/share`; use `%APPDATA%\Bifrost` on Windows.
- The Ubuntu fonts need a fallback (Segoe UI, Consolas).
- Tests that use `sh -c`, `pgrep`, PipeWire tools or a display should be marked `skipUnless(linux)`.

## Proposed structure: one codebase, platform backends

Do not fork. Add `bifrost/platform/` (`linux.py`, `windows.py`) and move the Linux-tied code
behind these small interfaces, **moving it rather than rewriting it** so Linux behaviour does
not change. The existing test suite (387 tests) is the safety net.

```python
# Audio output: Engine already takes a sink_factory and a Sink protocol (write, close).
make_sink(latency_ms: int = 100, target: str | None = None) -> Sink

# CD (portable logic stays in cd.py; the backend supplies only the hardware access)
find_drives() -> list[Drive]
read_toc(device) -> Toc                     # raises CDError with a user-safe message
read_track_to_wav(device, track, wav_path, cancel) -> None
eject(device) -> None

# USB
removable_volumes() -> list[Volume]
ensure_mounted(volume) -> str               # a drive letter needs no mounting
safely_remove(volume) -> None

# Live mode: the surface ui/live_view.py and app.Context already use
active, create_sink(), destroy_sink(), streams(exclude_pids), send(), release(), forget(),
follow(), sticky, moved, on_change, output_sink()
capture source for LiveEngine (what feeds the DSP chain)

# plus: paths.data_dir() / music_dir(), tools.ffmpeg_path() / ffprobe_path() / popen_flags(),
#       capabilities = {audio_out, usb, cd_rip, live} so the UI hides what a platform can't do
```

Stubs on Windows should raise `PlatformUnsupported("... needs the Windows backend")`, and the
UI should hide the Live tab, the Rip tab and "Export to USB" until each backend exists.

## Research (from the web; none of it has been tried yet)

- **Playback:** Python [`sounddevice`](https://python-sounddevice.readthedocs.io/) (PortAudio over WASAPI) is mature. [PyAudioWPatch](https://pypi.org/project/PyAudioWPatch/) is a PortAudio fork with WASAPI loopback. The PortAudio segfaults seen on the Linux machine came from PipeWire's ALSA shim, so they are not expected on Windows, but this is unverified there.
- **Live mode:** Windows 10 build 19041+ has *process loopback*, which captures one application's audio (or everything except one): [`ActivateAudioInterfaceAsync`](https://learn.microsoft.com/en-us/windows/win32/api/mmdeviceapi/nf-mmdeviceapi-activateaudiointerfaceasync) with `AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK`. Python wrappers exist: [ProcTap](https://github.com/m96-chan/ProcTap), [ProcessAudioCapture](https://github.com/tsubome/ProcessAudioCapture). **Open question:** capturing does not silence the original, so a *replacing* effect needs the application muted (for example through its audio session) or a virtual output device. Whether the capture is taken before or after the per-session volume is unknown. Measure this first; it decides whether live mode is feasible without extra drivers.
- **CD ripping:** [`DeviceIoControl` with `IOCTL_CDROM_RAW_READ`](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntddcdrm/ni-ntddcdrm-ioctl_cdrom_raw_read) from `ctypes` (2,352-byte raw sectors, open the drive as `\\.\E:`). Windows has no built-in error correction, so a verified-read strategy (read each range twice and compare, retry on mismatch) has to be written and checked against a real drive. Keep the existing safeguard: the ripped length must match the table of contents.
- **USB:** drive letters via `GetDriveType` or `psutil`; eject through the Windows API.
- **GTK4 on Windows:** [MSYS2](https://pygobject.readthedocs.io/en/latest/guide/deploy.html) (`mingw-w64-ucrt-x86_64-gtk4`, `python-gobject`) or gvsbuild; PyInstaller can freeze PyGObject apps. The Python version MSYS2 ships, and the installer size, are unverified.

## What cannot be verified without a Windows PC

Anything involving Windows audio output, a GTK window on Windows, a CD drive, a USB stick, or
the sound itself. GitHub's Windows runners are free and unlimited for public repositories and
can run the portable test subset (add `.github/workflows/ci.yml` with `ubuntu-latest` and
`windows-latest`, install numpy and ffmpeg), but they have no audio device or display.

## Windows test checklist (for the PC)

- [ ] Portable test subset passes; Linux-only tests are marked and skipped.
- [ ] Playing a FLAC and an MP3 through the DSP chain; the sweep and pulse are audible; no clicks when turning knobs.
- [ ] Output stays clean for several minutes (no static or dropouts); note any buffer/latency setting needed.
- [ ] Library, search and download work; downloaded files play.
- [ ] Export to a USB stick: original files, Original → MP3, Bilateral version into `audio_edits`; names are Windows-safe; tags and cover art present; the stick plays in the car.
- [ ] Safely remove drive works.
- [ ] CD info reads the table of contents and the MusicBrainz disc ID matches the Linux value for the same disc.
- [ ] A ripped track's length equals the disc's; ripping the same track twice gives identical audio.
- [ ] Live mode: answer the open question above before building the UI.
- [ ] No console windows flash while it runs; an installer or a PyInstaller build starts on a clean machine.

## Behaviour to preserve (learned the hard way on Linux)

- **Levels:** defaults must keep music at its original loudness (about −0.5 dB). `tests/test_loudness.py` pins this.
- **Tags and cover art must survive rendering.** A rendered file once lost all its tags and a car showed bare filenames. `tests/test_polish.py` pins this.
- **Exports are idempotent.** Re-exporting skips identical files; a bilateral version with new settings replaces its old one in `audio_edits`.
- **A track's identity is its metadata, not its filename.** Names can collide (a CD's run of `[silence]` tracks); the track number disambiguates.
- **Very short tracks (under 10 s) are usually silent CD padding.** The Rip tab unticks them and the export dialog offers to skip them.
- **Only characters a filesystem rejects are dropped from names.** Windows rejects `< > : " | ? *`; `P!nk` and `Can’t Take Me Home` are fine.
- **Live capture needs a cushion.** Feeding a real-time source straight into the output starves it; on Linux a 100 ms pre-fill took underruns from 578 per 25 s to 0. Expect a similar need on Windows, and measure it.
- **Screenshots are generated from synthetic data** (`tools/render_preview.py`) and must be checked by eye for paths and names before publishing.

## Decisions still open (ask the user)

1. **Which Windows features first?** Options: a command-line version only (render, search, download, export; fully testable in CI), or the desktop app without CD and live mode, or everything.
2. **Toolkit:** keep GTK4 (reuses about 2,000 lines of UI and the exact look; larger, fiddlier installer) or rewrite the interface in Qt (lighter, but two interfaces that can drift).
3. **Structure:** one codebase with platform backends (recommended above) or a separate Windows copy.

## Publishing

The public repository is `hstandish315/valhalla-tools`. Commit with the GitHub no-reply address,
never a personal email; scrub private paths, hostnames and third-party personal data from docs
and fixtures; and look at every changed screenshot before pushing.
