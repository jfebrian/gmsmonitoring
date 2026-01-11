# GMS Monitoring

`gms_monitor.py` is a small curses-based TUI for monitoring basic network stability to a single host (for example `www.youtube.com`). It periodically pings the target and shows loss, latency, jitter, and a traceroute summary.

## Features

- Periodic ICMP ping to a configurable host (hostname or IP)
- Recent-window vs whole-session statistics for:
  - Packet loss
  - Min/avg/max latency
  - Jitter (variation between successful pings)
- Quality classification (Excellent / Good / Fair / Poor) based on recent stats
- Short- and long-window loss summaries
- Alerts for delay spikes or high recent packet loss
- Integrated `traceroute` with:
  - Live streaming of output while running
  - Compact summary (hops, timeouts, max RTT)
  - Optional full hop-by-hop table with scroll
- Localization via external language files (`lang_en.txt`, `lang_id.txt`) with runtime language toggle
- TUI built with `curses`, suitable for running directly in a terminal

## Requirements

- Python 3
- `ping` and `traceroute` available on the system (tested on macOS / Unix-like systems)
- A terminal that supports `curses`

## Usage

From the repository root:

```bash
python3 gms_monitor.py [--lang LANG] [--host HOST]
```

Options:

- `--lang {en,id}` – UI language (default: `en`)
- `--host HOST` – target hostname or IP address to monitor (default: `www.youtube.com`)

Examples:

```bash
# Default: monitor www.youtube.com in English
python3 gms_monitor.py

# Monitor another host in Indonesian
python3 gms_monitor.py --host example.com --lang id
```

## On-screen information

The top section shows:

- **Status** – Monitoring / Paused, including the current target host (e.g. `Monitoring (www.youtube.com)`).
- **Ping now** – last ping result or timeout.
- **Quality** – textual quality rating with an optional explanation when help is enabled.
- **Window** – number of recent checks used for the "window" statistics.

The main metrics table compares **Window** vs **Session** for:

- **Loss** – recent and session packet loss (percentage and counts)
- **Ping** – min/avg/max latency
- **Jitter** – average difference between consecutive successful pings

Extra lines show short- and long-window loss summaries.

The bottom section shows traceroute status, an optional summary line, and a hop-by-hop table (either compact or full, depending on mode).

## Keyboard controls

General controls:

- `P` – Pause monitoring (stop sending pings)
- `R` – Resume monitoring
- `T` – Run traceroute (if not already running)
- `F` – Toggle between summary and full traceroute table view
- `L` – Toggle UI language (cycle through supported languages)
- `H` – Toggle extra help / explanations for some fields
- `K` – Toggle visibility of the controls/keys guide
- `↑ / ↓` – Scroll traceroute output when in full view and more lines are available
- `+` – Increase the stats window size (up to the history limit)
- `-` – Decrease the stats window size (down to the minimum)
- `Q` – Quit the application

## Localization

Text strings are loaded from `lang_*.txt` files in the same directory as the script. The default language is English (`lang_en.txt`), and other languages (e.g. Indonesian in `lang_id.txt`) can override any subset of keys.

You can:

- Select a language at startup with `--lang`
- Toggle languages at runtime with the `L` key

To add another language, create a corresponding `lang_xx.txt` file and add the code to `SUPPORTED_LANGS` in `gms_monitor.py`.
