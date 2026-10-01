# Valhalla — Mjölnir System Monitor

A desktop dashboard for CPU, GPU, memory and storage, in a Norse storm theme.
Four hand-painted radial gauges over a detail deck, with procedural lightning
that fires when a subsystem is under real pressure.

![Valhalla dashboard](docs/preview.png)

| Channel | Name | Why |
| --- | --- | --- |
| CPU | **HUGINN** | Odin's raven, *thought* — the machine's mind |
| GPU | **SURTR** | the fire giant — heat and light |
| Memory | **MÍMIR** | the well of *memory* |
| Storage | **FÁFNIR** | the dragon on the hoard |

For step-by-step installation, reading the dashboard and troubleshooting,
see **[SETUP.md](SETUP.md)**.

## Running it

```bash
./valhalla-monitor
```

No install step and no virtualenv — it runs from the source tree.

To add it to the GNOME app grid (per-user only, asks before writing, prints its
own revert instructions). The entry launches with `--calm --fps 15`, the frugal
profile described below — edit `FLAGS` in the script to change that:

```bash
./install-desktop-entry.sh
```

### Options

| Flag | Default | Effect |
| --- | --- | --- |
| `--interval SECONDS` | `1.0` | hardware sampling period |
| `--fps N` | `30` | repaint cap; `60` is smoother and costs roughly double |
| `--calm` | off | drops the ambient gauge animation, so the window repaints only when a reading changes — near-zero idle CPU |
| `--fullscreen` | off | start fullscreen |

Keys: **F11** fullscreen, **Esc** leave fullscreen, **Ctrl+Q** quit.

## Requirements

Nothing beyond the packages below is needed; there is no virtualenv and no build step.

- Python 3.14, PyGObject / **GTK 4.22**, **pycairo**
- **psutil** for CPU, memory, filesystem and I/O counters
- **`nvidia-smi`** for GPU telemetry (optional — the GPU gauge shows `OFFLINE`
  and the rest of the dashboard carries on without it)

GTK4 rather than Qt because PyQt6 wheels for Python 3.14 were unreliable when
this was built, while GTK4 + Cairo ships with a stock Ubuntu desktop.

## What it reads, and from where

- **CPU** — `psutil.cpu_percent(percpu=True)` for all 24 threads; package
  temperature from **k10temp `Tctl`**, with `Tccd1`/`Tccd2` shown separately
  since the 9900X has two core dies. No `lm-sensors` needed.
- **GPU** — one batched `nvidia-smi` query (~20 ms) for utilisation, VRAM,
  temperature, power against cap, SM/memory clocks and fan.
- **Memory** — `virtual_memory` / `swap_memory`; "used" is `total - available`,
  which is the number that matches what you can actually still allocate,
  rather than counting reclaimable cache as used.
- **Storage** — real filesystems only; the ~30 squashfs snap mounts and every
  `loop*` device are excluded from both the mount list and the I/O totals.
  Drive temperature is resolved through the actual device chain
  (`filesystem → LVM → dm-crypt → partition → disk`) rather than assumed, so the
  temperature shown against `/` is the drive that genuinely backs it.

## Design notes

**Two clocks, deliberately decoupled.** Hardware is sampled once a second on a
worker thread — faster is meaningless for CPU deltas and wasteful for
`nvidia-smi` — while the GTK frame clock eases every on-screen value toward the
last sample. The dashboard therefore glides continuously instead of stepping
once a second, and a slow or stalled probe can never stutter the animation.

**Cost, measured rather than assumed.** A monitor that burns a core is
self-defeating, so the running process was profiled, not estimated. Two things
turned out to be real and one turned out not to be:

| Fix | Effect |
| --- | --- |
| 49 gauge ticks batched into 4 strokes | 0.9 ms → 0.04 ms per gauge |
| gauge plate cached to a surface | 2.0 ms → 1.4 ms per gauge |
| detail panels repaint only while a value is moving | idle panels cost nothing |
| thread count from `/proc/loadavg` instead of walking `/proc` | 8 ms → 0.02 ms per sample |
| caching Pango layouts | ~3 points; text was *not* the bottleneck |

Profiling killed the obvious theory. The dominant cost is not Python overhead
at all — it is Cairo software-rasterising a screenful of gradients and glows.
That sets the floor, and the honest numbers on the author's Ryzen 9 9900X are:

| Configuration | One core | Whole machine (24 threads) |
| --- | --- | --- |
| `--fps 60` | 46% | 1.9% |
| `--fps 30` (default) | 25% | 1.0% |
| `--fps 15` | 14% | 0.6% |
| `--calm` | 13% | 0.6% |
| `--calm --fps 10 --interval 2` | **3%** | 0.13% |

Attributing the saving to each flag separately (percent of one core):

| | alone | combined |
| --- | --- | --- |
| default | 25% | |
| `--interval 2` | 23% | sampling is cheap; this buys almost nothing on its own |
| `--calm` | 11% | |
| `--fps 10` | 9% | |
| `--calm --fps 15` | | **8% — the sweet spot, and what the desktop entry uses** |
| `--calm --fps 10` | | 6% |
| `--calm --fps 10 --interval 2` | | 3% |

The repaint rate and the ambient animation are what cost; the sampling interval
barely registers — and `--interval` is the one flag with a real accuracy price.
`psutil.cpu_percent` is a delta since the previous call, so at `--interval 2`
every CPU figure is a two-second average: a 0.5 s burst to 100% displays as 25%.
Disk I/O rates flatten the same way. That hides exactly what you opened a monitor
to see, for about one point of CPU.

Don't drop below **15 fps**. The lightning flicker envelope runs at 7.5 Hz, so
15 fps is its Nyquist floor; at 10 fps the strikes alias into a random stutter
instead of reading as flicker. `--fps 10` saves under a point over `--fps 15`
and breaks the theme's signature effect.

For a dashboard parked on a second monitor all day:

```bash
valhalla-monitor --calm --fps 15
```

The easing "settled" threshold is deliberately coarse (0.15%). Live CPU and I/O
readings jitter constantly, and with a tight threshold the easing never finishes,
so the panels repaint forever over changes too small to see.

Animation is driven by a GLib timeout rather than a GTK tick callback: a tick
callback keeps the frame clock running continuously even on frames where nothing
is queued for redraw, and it is also tied to the display's refresh rate — this
machine's frame clock runs at ~100 Hz, so throttling by counting frames would
silently paint at the wrong rate.

**Painting.** Everything is Cairo, not GTK CSS: the look depends on glows,
sweep gradients and runework that CSS cannot express. Cairo has no conic
gradient, so each gauge arc is built from short arc segments with interpolated
colour and overlapping ends to hide the seams; it has no blur either, so every
bloom is stacked strokes, wide-and-faint under narrow-and-bright. The Elder
Futhark glyphs are drawn as line segments rather than typed, because no
Runic-capable font is installed here — and vector runes take the same glow
treatment as the rest of the chrome.

**Yggdrasil** is the application's mark, and the icon and the header banner draw
the *same* tree from `theme.draw_yggdrasil` rather than each having their own:
a tapered trunk, three canopy limbs and three splayed roots. The icon adds a
world-ring that echoes the gauges and a strike through the crown; the banner
uses a shallower recursion (canopy depth 4, roots depth 2) with bronze roots,
because at 60 px the finer branching reads as noise and dark roots read as a
smudge. The recursion is deliberately shallow and the limbs deliberately thick —
the mark has to survive being scaled to 32 px in the app grid, where silhouette
beats detail.

The tree geometry is generated once in a unit box and cached. The banner now
repaints on ~95% of frames because of the lightning, so rebuilding a recursive
tree per frame would be pure waste.

The **banner** throws three kinds of strike so it does not read as one looping
effect: bolts running along the bottom rail, bolts falling from the top edge
into it, and short arcs behind the title, each landing with a flash and an
afterglow that bleeds up from the rail.

**Lightning** is recursive midpoint displacement: push a segment's midpoint
along its normal, recurse, and forks are the same process on a shorter, dimmer
child. Strike frequency is driven by how far past 75% a metric sits, so an idle
machine is calm and a loaded one crackles.

**Colour carries meaning.** Each channel owns a hue (cyan / amber / violet /
sea-green). Two thresholds, and they are not the same one: past **75%** the
colour warms *and* the ring starts throwing lightning, with strike frequency
scaling by how far past it you are; past **88%** the colour goes red. The warm
blend is capped well short of pure amber, so a channel at 80% still reads as
*itself*. Only genuine danger goes red.

## Layout

```
tools/render_preview.py   offscreen renderer — Wayland blocks desktop capture
tools/make_icon.py        generates the app icon
valhalla/theme.py         palette, typography, runes, Cairo primitives
valhalla/metrics.py       sampler thread, device resolution, formatters
valhalla/lightning.py     procedural bolts
valhalla/gauges.py        the four radial gauges
valhalla/panels.py        header bar and the detail panels
valhalla/app.py           window assembly, animation clock, CLI
```

`tools/render_preview.py` drives the widgets' draw functions straight onto an
image surface using the real layout geometry. That exists because Wayland blocks
desktop capture on the author's Wayland desktop and GTK4 dropped `Gtk.OffscreenWindow`, so it is
the only way to actually *look* at the dashboard while iterating on it:

```bash
python3 tools/render_preview.py out.png --load 0.9   # synthetic load
python3 tools/render_preview.py out.png              # live values
```
