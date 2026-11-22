# iphone-top

htop for an iPhone plugged into your computer. Live CPU, memory, FPS, GPU and per-process network, from Linux, Windows or macOS. No Xcode.

![iphone-top showing an iPhone X: CPU and per-core meters, memory split, FPS and GPU, and a process table sorted by CPU](https://raw.githubusercontent.com/Cassian433/iphone-top/main/docs/demo.gif)

<sub>An iPhone X on iOS 16.7.16 sitting on a desk, recorded with `--json` and replayed with only Apple's own processes kept. FPS reads 0 because nothing on screen was moving.</sub>

## Install

```sh
uvx --from git+https://github.com/Cassian433/iphone-top iphone-top   # run it without installing
pipx install git+https://github.com/Cassian433/iphone-top            # or keep it around
```

It isn't on PyPI yet, so this installs from the repository. Python 3.10 or newer. It installs [pymobiledevice3](https://github.com/doronz88/pymobiledevice3) and [Textual](https://github.com/Textualize/textual).

## Use

```sh
iphone-top                          # the TUI
iphone-top --udid 00008020-...      # pick a device when several are plugged in
iphone-top --json | jq ...          # one JSON sample per line
iphone-top --json -n 60 > run.ndjson && iphone-top --replay run.ndjson
```

| Key | |
| --- | --- |
| `q` | quit |
| `s` | cycle the sort: CPU, memory, RX, TX, PID, name |
| `/` | filter by name or PID (Enter keeps it, Esc clears it) |
| `a` | only what iOS lists as apps |
| `p` | pause the display (sampling keeps going) |
| arrows, PgUp/PgDn | move; the highlight follows the process you picked, and back on the top row it follows whatever is on top |

The top half:

- **CPU** is the whole device; the numbered meters are the cores. The graph under it is a fixed 0-100% scale, one column per sample.
- **Mem** is app + wired + compressed, the same split Activity Monitor uses. Cached file pages are counted separately.
- **FPS** is Core Animation frames per second for the whole display. It drops to 0 when nothing is redrawing, which is normal, not a hang. **GPU** is the GPU's device utilization.
- The top line has the model, iOS version, uptime, battery level, whether it's on power, and battery temperature.

The table:

- **CPU%** is percent of one core, as in `top`, so a busy multithreaded process can go past 100.
- **MEM** is the physical footprint, the number Xcode's memory gauge and the jetsam killer look at.
- **RX/s** and **TX/s** are bytes per second for that process. `·` means idle, `-` means not measured yet (the first two seconds, or a process that just started).
- Names iOS lists as apps are bold (apps, plus some of their extensions). `DTServiceHub` and `sysmond` are marked `(sampling)`: that's the device side of iphone-top.

Run `iphone-top --help` for the rest: `--interval MS` (default 1000), `--no-net`, `--count N`, `--speed` for replays, `--usbmux` for a non-default usbmuxd.

## JSON

`--json` prints a `device` line, then a `sample` line per interval with every process, busiest first:

```json
{"name":"iPhone","model":"iPhone X","product_type":"iPhone10,6","ios_version":"16.7.16","build":"20H392","boot_time":1788405252.490158,"type":"device"}
{"time":1791721939.84,"cpu":{"total":56.98,"cores":[86.0,87.0,33.67,38.78,47.47,48.98]},"memory":{"total":2961506304,"used":2037432320,"app":784531456,"wired":731611136,"compressed":521289728,"cached":661667840,"free":262406144},"disk_read_rate":0.0,"disk_write_rate":0.0,"fps":0,"gpu":0,"battery":{"level":97,"charging":false,"external_power":true,"temperature":29.29},"processes":[{"pid":82349,"name":"SpringBoard","cpu":1.04,"memory":83199576,"threads":20,"rx_rate":58.69,"tx_rate":1464.43,"app":false}],"type":"sample"}
```

(That second line is trimmed to one process.) Bytes are bytes, rates are bytes per second, times are Unix seconds.

```sh
# CPU and FPS once a second
iphone-top --json | jq -c 'select(.type == "sample") | {time, cpu: .cpu.total, fps}'

# one app's footprint over a test run
iphone-top --json | jq -c 'select(.type == "sample") | .processes[] | select(.name == "MyApp") | {cpu, memory}'
```

A file written with `--json` plays back in the TUI with `--replay`.

## Requirements

- A USB cable and a phone that trusts this computer (unlock it and tap Trust).
- **Linux:** `usbmuxd` running. **Windows:** iTunes or the Apple Devices app, for the USB driver. **macOS:** nothing extra.
- **iOS 16 and older:** Developer Mode on (Settings > Privacy & Security > Developer Mode) and the Developer Disk Image mounted:

  ```sh
  pymobiledevice3 mounter auto-mount
  ```

  iphone-top doesn't mount anything itself. If the image or Developer Mode is missing, it says which one and what to run.
- **iOS 17 and newer:** Apple moved the instruments services behind a RemoteXPC tunnel. On 17.4+ iphone-top opens pymobiledevice3's no-root userspace tunnel by itself. On 17.0-17.3 under Linux or Windows, start a tunnel first and point iphone-top at it:

  ```sh
  sudo pymobiledevice3 remote tunneld    # Windows: an administrator prompt, no sudo
  iphone-top --tunnel
  ```

  `--rsd HOST PORT` takes the address printed by `pymobiledevice3 remote start-tunnel`. The Developer Disk Image still needs `pymobiledevice3 mounter auto-mount`.

  **I haven't tested iOS 17 or later.** My phones are on iOS 16. The 17+ path uses the same pymobiledevice3 calls its own CLI uses, but until someone runs it, treat it as untested. A report in an issue would help a lot. This prints three samples without your device's name or its process list:

  ```sh
  iphone-top --json -n 3 | jq -c 'del(.name) | if .type == "sample" then .processes |= length else . end'
  ```

## How it works

Everything comes from the services Xcode's Instruments uses, reached through [pymobiledevice3](https://github.com/doronz88/pymobiledevice3), all on one DTX connection:

- **sysmontap**: per-core CPU load, VM page counts, disk counters, and per-process CPU, footprint and thread count. iphone-top asks for six process attributes instead of the ~74 available. One of them is `procAge`, because sysmontap leaves out any process whose requested values didn't change, and without an attribute that always changes the idle processes vanish from the list.
- **graphics.opengl**: `CoreAnimationFramesPerSecond` and `Device Utilization %`, about once a second.
- **NetworkStatistics** (Xcode's network gauge): lifetime byte counters per PID. Rates come from consecutive totals. The provider's own `.delta` fields don't track them: they were often 0 while the totals grew.
- **runningProcesses**: full names (sysmontap cuts them at 31 characters), which processes are apps, and launchd's start date for the uptime.
- **lockdown and the diagnostics relay**: battery level, charging state and battery temperature, every 30 seconds.

I added the per-process NetworkStatistics ([#1998](https://github.com/doronz88/pymobiledevice3/pull/1998)) and FPS ([#1999](https://github.com/doronz88/pymobiledevice3/pull/1999)) support to pymobiledevice3 before writing this. The rest of the heavy lifting is pymobiledevice3's.

## Limitations

- **Watching costs something.** On an iPhone X, DTServiceHub runs at about 65% of one core while iphone-top is attached, and sysmond at about 3%. They're in the table so you can account for them.
- **No system-wide network meter.** The device side of the USB connection runs over loopback, so the device's interface counters moved by the same byte count in and out every second, and most of it was iphone-top's own traffic (about 700 KB/s). For the same reason, `kernel_task` RX and `DTServiceHub` TX in the table are mostly iphone-top.
- **FPS is the whole display**, not one app.
- **No thermal state.** I didn't find iOS's thermal state exposed over lockdown or DVT, so the header shows battery temperature instead.
- **Memory assumes 16 KiB pages**, which current iPhones use (checked on an iPhone X).
- **Untested:** iOS 17+, devices over Wi-Fi, iPads, macOS and Windows hosts. Tested on Linux (Ubuntu, Python 3.10 and 3.14) with an iPhone X on iOS 16.7.16 over USB.
- It only reads. It never launches, kills or signals processes, changes settings, or mounts images.

If something fails in a way the message doesn't explain, run with `IPHONE_TOP_DEBUG=/tmp/iphone-top.log`. The log holds pymobiledevice3's debug output, which includes your device's name and UDID, so look through it before attaching it to an issue.

## Development

```sh
uv sync
uv run pytest            # no device needed; uses samples recorded from an iPhone X
uv run ruff check . && uv run ruff format --check .
uv run pyright
```

`scripts/record_fixtures.py` re-records the test fixtures from a connected device. It keeps only Apple system processes (by executable path and bundle ID) and replaces the device name, so a recording doesn't describe what's installed on the phone. The demo GIF is a 35-sample recording made the same way, replayed under [asciinema](https://asciinema.org) and rendered with [agg](https://github.com/asciinema/agg).

## License

MIT
