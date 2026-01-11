#!/usr/bin/env python3
import argparse
import curses
import threading
import subprocess
import time
import re
import os
from collections import deque

# Localization ---------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_LANG = "en"
SUPPORTED_LANGS = ["en", "id"]  # extend as needed
CURRENT_LANG = DEFAULT_LANG
CURRENT_STRINGS: dict[str, str] = {}
_DEFAULT_STRINGS_CACHE: dict[str, str] | None = None


def load_language_file(lang: str) -> dict:
    """Load lang_XX.txt from the script directory.

    Format: KEY = value
    - Lines starting with # or empty lines are ignored.
    - First '=' splits key and value.
    """
    filename = f"lang_{lang}.txt"
    path = os.path.join(SCRIPT_DIR, filename)
    strings: dict = {}

    if not os.path.exists(path):
        return strings

    with open(path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.lstrip()
            if key:
                strings[key] = value
    return strings


def set_language(lang: str):
    global CURRENT_LANG, CURRENT_STRINGS, _DEFAULT_STRINGS_CACHE
    if lang not in SUPPORTED_LANGS:
        lang = DEFAULT_LANG
    CURRENT_LANG = lang

    if _DEFAULT_STRINGS_CACHE is None:
        _DEFAULT_STRINGS_CACHE = load_language_file(DEFAULT_LANG)

    if lang == DEFAULT_LANG:
        CURRENT_STRINGS = dict(_DEFAULT_STRINGS_CACHE)
    else:
        localized = load_language_file(lang)
        merged = dict(_DEFAULT_STRINGS_CACHE)
        merged.update(localized)
        CURRENT_STRINGS = merged


def cycle_language():
    """Cycle to the next available language in SUPPORTED_LANGS."""
    global CURRENT_LANG
    if not SUPPORTED_LANGS:
        return
    try:
        current_index = SUPPORTED_LANGS.index(CURRENT_LANG)
    except ValueError:
        current_index = 0
    next_index = (current_index + 1) % len(SUPPORTED_LANGS)
    set_language(SUPPORTED_LANGS[next_index])


def tr(key: str, **kwargs) -> str:
    """Translate a key using CURRENT_STRINGS, falling back to the key itself."""
    template = CURRENT_STRINGS.get(key, key)
    try:
        return template.format(**kwargs)
    except Exception:
        return template


DEFAULT_TARGET_HOST = "www.youtube.com"
PING_INTERVAL_SECONDS = 1.0
PING_HISTORY_LENGTH = 300  # how many samples to keep for graphs
LOSS_HISTORY_LENGTH = 300

MIN_WINDOW_SIZE = 10       # minimum number of recent checks
DEFAULT_WINDOW_SIZE = 60   # default "time window" in checks (≈ seconds)


class MonitorState:
    def __init__(self, target_host: str):
        self.lock = threading.Lock()

        # Target being monitored (host name or IP address)
        self.target_host = target_host

        # Control flags
        self.running = True          # overall program running
        self.monitoring = True       # whether ping loop is active
        self.show_help = False       # descriptions hidden by default
        self.show_traceroute_full = False  # summary by default; F toggles details
        self.traceroute_scroll = 0         # scroll position for traceroute output
        self.show_controls = False         # whether to show the keys guide (hidden by default)

        # Ping stats (overall)
        self.total_sent = 0
        self.total_recv = 0
        self.last_ping_ms = None

        # Overall RTT stats (successful pings only)
        self.total_success = 0
        self.success_rtt_sum = 0.0
        self.min_rtt_all = None
        self.max_rtt_all = None
        self.last_success_rtt_all = None
        self.jitter_sum_all = 0.0
        self.jitter_count_all = 0

        # History
        self.ping_history = deque(maxlen=PING_HISTORY_LENGTH)  # float ms or None for loss
        self.loss_history = deque(maxlen=LOSS_HISTORY_LENGTH)  # float % (overall, for reference)

        # Window used for "recent" stats and graphs (in checks)
        self.window_size = DEFAULT_WINDOW_SIZE

        # Traceroute
        self.traceroute_lines = []
        self.traceroute_running = False
        self.last_traceroute_error = None
        self.traceroute_summary = None
        self.last_traceroute_ts = None


def run_ping(host: str, timeout: float = 5.0):
    """
    Run a single ping and return (success: bool, rtt_ms: float|None).
    Uses very generic flags to work on macOS and most Unix systems.
    """
    try:
        # -c 1 -> send one packet; -n -> numeric output
        proc = subprocess.run(
            ["ping", "-n", "-c", "1", host],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except Exception:
        return False, None

    if proc.returncode != 0:
        return False, None

    # Look for "time=XX.X ms" in the output
    match = re.search(r"time[=<]([\d.]+)\s*ms", proc.stdout)
    if not match:
        return True, None  # success but no RTT parsed

    try:
        rtt = float(match.group(1))
    except ValueError:
        rtt = None
    return True, rtt


def ping_worker(state: MonitorState, interval: float):
    while True:
        with state.lock:
            if not state.running:
                break
            monitoring = state.monitoring
            host = state.target_host

        if monitoring:
            start_time = time.time()
            success, rtt = run_ping(host)
            elapsed = time.time() - start_time

            with state.lock:
                state.total_sent += 1
                if success:
                    state.total_recv += 1
                    state.last_ping_ms = rtt

                    # Update overall RTT stats if we have a numeric RTT
                    if rtt is not None:
                        if state.total_success == 0:
                            state.min_rtt_all = rtt
                            state.max_rtt_all = rtt
                        else:
                            if state.min_rtt_all is None or rtt < state.min_rtt_all:
                                state.min_rtt_all = rtt
                            if state.max_rtt_all is None or rtt > state.max_rtt_all:
                                state.max_rtt_all = rtt

                        if state.last_success_rtt_all is not None:
                            delta = abs(rtt - state.last_success_rtt_all)
                            state.jitter_sum_all += delta
                            state.jitter_count_all += 1
                        state.last_success_rtt_all = rtt

                        state.success_rtt_sum += rtt
                        state.total_success += 1
                else:
                    state.last_ping_ms = None

                sent = state.total_sent
                recv = state.total_recv
                overall_loss_pct = 0.0 if sent == 0 else (1.0 - (recv / sent)) * 100.0

                # For ping history, None == timeout / packet lost
                state.ping_history.append(rtt if success else None)
                state.loss_history.append(overall_loss_pct)

            sleep_time = max(0.0, interval - elapsed)
            time.sleep(sleep_time)
        else:
            # When paused, just check again shortly
            time.sleep(0.1)


def build_traceroute_summary(lines: list[str]) -> str | None:
    """Build a compact summary of a completed traceroute.

    Extracts number of hops, max observed delay, and which hops had timeouts.
    """
    if not lines:
        return None

    hop_re = re.compile(r"^\s*(\d+)\s+")
    ms_re = re.compile(r"([\d.]+)\s*ms")

    hop_numbers: list[int] = []
    timeout_hops: list[int] = []
    max_ms: float | None = None

    for line in lines[1:]:  # skip potential header
        m = hop_re.match(line)
        if not m:
            continue
        hop = int(m.group(1))
        hop_numbers.append(hop)

        if "*" in line:
            timeout_hops.append(hop)

        for ms_match in ms_re.finditer(line):
            try:
                val = float(ms_match.group(1))
            except ValueError:
                continue
            if max_ms is None or val > max_ms:
                max_ms = val

    if not hop_numbers:
        return None

    hops = max(hop_numbers)
    if max_ms is None:
        max_ms = 0.0

    if not timeout_hops:
        timeout_info = tr("TRACE_TIMEOUT_INFO_NONE")
    else:
        hop_list = ", ".join(str(h) for h in timeout_hops)
        timeout_info = tr("TRACE_TIMEOUT_INFO_SOME", hops=hop_list)

    return tr("TRACE_FINAL_SUMMARY", hops=hops, max_ms=max_ms, timeout_info=timeout_info)


def build_traceroute_table(lines: list[str]) -> list[str]:
    """Format traceroute output as a simple table: one line per hop.

    Columns: Hop | Host/IP | RTTs (min/avg/max or timeout).
    """
    if not lines:
        return []

    hop_re = re.compile(r"^\s*(\d+)\s+")
    ms_re = re.compile(r"([\d.]+)\s*ms")

    rows: list[str] = []

    # Header row
    header = f"{'Hop':>3}  {'Host / IP':<40}  RTTs (ms)"
    rows.append(header)

    # Parse each hop line
    for line in lines:
        m = hop_re.match(line)
        if not m:
            continue
        hop = int(m.group(1))
        rest = line[m.end():].strip()

        host = "?"
        ip = ""

        # Try to extract host and IP from "host (ip)" pattern
        if "(" in rest and ")" in rest:
            before, after = rest.split("(", 1)
            host = before.strip() or "?"
            ip = after.split(")", 1)[0].strip()
        else:
            # Fallback: first token is host or IP
            parts = rest.split()
            if parts:
                host = parts[0]

        host_ip = host
        if ip and ip not in host:
            host_ip = f"{host} ({ip})"

        rtts: list[float] = []
        for ms_match in ms_re.finditer(line):
            try:
                val = float(ms_match.group(1))
            except ValueError:
                continue
            rtts.append(val)

        if rtts:
            mn = min(rtts)
            mx = max(rtts)
            avg = sum(rtts) / len(rtts)
            rtt_text = f"min {mn:.1f} / avg {avg:.1f} / max {mx:.1f}"
        else:
            rtt_text = "timeout"

        row = f"{hop:>3}  {host_ip:<40.40}  {rtt_text}"
        rows.append(row)

    return rows


def traceroute_worker(state: MonitorState):
    """Run traceroute and stream output lines into state.traceroute_lines.

    This gives the user ongoing feedback while traceroute is in progress.
    """
    with state.lock:
        if state.traceroute_running:
            return
        state.traceroute_running = True
        state.last_traceroute_error = None
        state.traceroute_lines = []
        state.traceroute_summary = None
        # Record start time for this run
        state.last_traceroute_ts = time.time()
        host = state.target_host
    try:
        # Use Popen so we can stream output line by line.
        proc = subprocess.Popen(
            ["traceroute", host],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except Exception as e:
        with state.lock:
            state.traceroute_lines = []
            state.last_traceroute_error = f"Error starting traceroute: {e!r}"
            state.traceroute_running = False
        return

    lines = []
    start_time = time.time()
    error_msg = None

    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            lines.append(line)
            with state.lock:
                state.traceroute_lines = list(lines)

            # Enforce an overall timeout so traceroute cannot hang forever.
            if time.time() - start_time > 300:
                proc.kill()
                error_msg = "traceroute timed out after 300 seconds"
                break

        # Wait for process to exit (short timeout just for cleanup)
        try:
            retcode = proc.wait(timeout=5)
        except Exception:
            retcode = None

        if error_msg is None and retcode not in (0, None):
            error_msg = f"traceroute exited with code {retcode}"
    except Exception as e:
        error_msg = f"Error running traceroute: {e!r}"

    summary = build_traceroute_summary(lines) if error_msg is None else None

    with state.lock:
        state.traceroute_lines = lines
        state.last_traceroute_error = error_msg
        state.traceroute_running = False
        state.traceroute_summary = summary
        state.last_traceroute_ts = time.time()


# For extra loss windows (used in admin stats)
SHORT_WINDOW_SIZE = 10     # ~10 seconds
LONG_WINDOW_SIZE = 600     # ~10 minutes with 1s interval



def format_ping_value(ms):
    """Format a ping value in ms, clipping very large values.

    - None -> "-" (used only in aggregated contexts if ever needed)
    - <= 999.0 -> one decimal, e.g. "23.4"
    - > 999.0 -> ">999" (so worst-case "min >999 / avg >999 / max >999 ms" fits)
    """
    if ms is None:
        return "-"
    try:
        value = float(ms)
    except Exception:
        return str(ms)
    if value > 999.0:
        return ">999"
    return f"{value:.1f}"




def compute_recent_stats(ping_history, window_size):
    """
    Compute recent packet loss and ping statistics over the last `window_size` checks.
    Returns (recent_loss_pct, recent_avg_ping_ms, recent_count, recent_lost,
             recent_min_ping_ms, recent_max_ping_ms, jitter_ms).

    Jitter here is a simple average of the absolute difference between
    consecutive successful pings inside the window.
    """
    if window_size <= 0:
        return 0.0, None, 0, 0, None, None, None

    recent = ping_history[-window_size:]
    recent_count = len(recent)
    if recent_count == 0:
        return 0.0, None, 0, 0, None, None, None

    successes = [v for v in recent if v is not None]
    recent_sent = recent_count
    recent_success = len(successes)
    recent_lost = recent_sent - recent_success
    recent_loss_pct = (recent_lost / recent_sent) * 100.0 if recent_sent > 0 else 0.0
    recent_avg_ping = (
        sum(successes) / recent_success if recent_success > 0 else None
    )
    recent_min_ping = min(successes) if recent_success > 0 else None
    recent_max_ping = max(successes) if recent_success > 0 else None

    # Jitter: average absolute delta between consecutive successful pings
    if recent_success > 1:
        deltas = [
            abs(successes[i] - successes[i - 1])
            for i in range(1, recent_success)
        ]
        jitter_ms = sum(deltas) / len(deltas)
    else:
        jitter_ms = None

    return (
        recent_loss_pct,
        recent_avg_ping,
        recent_count,
        recent_lost,
        recent_min_ping,
        recent_max_ping,
        jitter_ms,
    )


def draw_ui(stdscr, state: MonitorState, admin_mode: bool):
    stdscr.clear()
    max_y, max_x = stdscr.getmaxyx()

    with state.lock:
        monitoring = state.monitoring
        last_ping_ms = state.last_ping_ms
        total_sent = state.total_sent
        total_recv = state.total_recv
        ping_history = list(state.ping_history)
        loss_history = list(state.loss_history)
        traceroute_lines = list(state.traceroute_lines)
        traceroute_running = state.traceroute_running
        traceroute_error = state.last_traceroute_error
        traceroute_summary = state.traceroute_summary
        last_traceroute_ts = state.last_traceroute_ts
        window_size = state.window_size
        show_help = state.show_help
        show_traceroute_full = state.show_traceroute_full
        traceroute_scroll = state.traceroute_scroll
        show_controls = state.show_controls

        total_success = state.total_success
        success_rtt_sum = state.success_rtt_sum
        min_rtt_all = state.min_rtt_all
        max_rtt_all = state.max_rtt_all
        jitter_sum_all = state.jitter_sum_all
        jitter_count_all = state.jitter_count_all
        target_host = state.target_host

    # Header (not localized on purpose)
    title = "GMS Monitoring"
    stdscr.addnstr(0, 0, title[:max_x], max_x)

    # Instructions / controls (toggleable)
    if show_controls:
        # Table header: Key | Action (no separate title line)
        header_line = f"{'Key':<8}{tr('KEYS_ACTION_HEADER')}"
        stdscr.addnstr(2, 0, header_line[:max_x], max_x)

        # Individual key rows
        key_rows = [
            ("P", tr("KEY_ACTION_P")),
            ("R", tr("KEY_ACTION_R")),
            ("T", tr("KEY_ACTION_T")),
            ("F", tr("KEY_ACTION_F")),
            ("L", tr("KEY_ACTION_L")),
            ("H", tr("KEY_ACTION_H")),
            ("K", tr("KEY_ACTION_K")),
            ("↑/↓", tr("KEY_ACTION_SCROLL")),
            ("Q", tr("KEY_ACTION_Q")),
        ]

        row_y = 3
        for key, desc in key_rows:
            if row_y >= max_y:
                break
            line = f"{key:<8}{desc}"
            stdscr.addnstr(row_y, 0, line[:max_x], max_x)
            row_y += 1

        controls_bottom_y = row_y - 1
    else:
        hint = tr("CONTROLS_HINT")
        stdscr.addnstr(2, 0, hint[:max_x], max_x)
        controls_bottom_y = 2

    if admin_mode:
        admin_controls = tr("ADMIN_CONTROLS")
        stdscr.addnstr(controls_bottom_y + 1, 0, admin_controls[:max_x], max_x)
        controls_bottom_y += 1

    # Top info table: status, ping now, quality, and window
    if show_controls:
        top_start_y = controls_bottom_y + 2
    else:
        # Leave one blank line between the hint ("Press K to see all keys") and the top info table
        top_start_y = controls_bottom_y + 2

    status_key = "STATUS_MONITORING" if monitoring else "STATUS_PAUSED"
    status_text = f"{tr(status_key)} ({target_host})"

    top_row_y = top_start_y
    label_width = 12

    def add_top_row(label: str, value: str):
        nonlocal top_row_y
        if top_row_y >= max_y:
            return
        line = f"{label:<{label_width}} {value}"
        stdscr.addnstr(top_row_y, 0, line[:max_x], max_x)
        top_row_y += 1

    # Status row
    add_top_row(tr("STATUS_LABEL"), status_text)

    # Ping now row
    if last_ping_ms is None:
        ping_now_val = tr("PING_NOW_VALUE_TIMEOUT")
    else:
        ping_now_val = f"{format_ping_value(last_ping_ms)} ms"
    add_top_row(tr("PING_NOW_LABEL"), ping_now_val)

    # Recent stats based on window
    (
        recent_loss_pct,
        recent_avg_ping,
        recent_count,
        recent_lost,
        recent_min_ping,
        recent_max_ping,
        jitter_ms,
    ) = compute_recent_stats(ping_history, window_size)

    # Derive quality label from recent stats
    quality_label = tr("QUALITY_UNKNOWN")
    quality_reason = tr("QUALITY_UNKNOWN_REASON")
    if recent_count >= max(20, window_size // 2) and recent_avg_ping is not None:
        if recent_loss_pct < 1.0 and recent_avg_ping < 40 and (jitter_ms is None or jitter_ms < 5):
            quality_label = tr("QUALITY_EXCELLENT")
            quality_reason = tr("QUALITY_EXCELLENT_REASON")
        elif recent_loss_pct < 2.0 and recent_avg_ping < 80:
            quality_label = tr("QUALITY_GOOD")
            quality_reason = tr("QUALITY_GOOD_REASON")
        elif recent_loss_pct < 5.0 and recent_avg_ping < 150:
            quality_label = tr("QUALITY_FAIR")
            quality_reason = tr("QUALITY_FAIR_REASON")
        else:
            quality_label = tr("QUALITY_POOR")
            quality_reason = tr("QUALITY_POOR_REASON")

    lost_overall = total_sent - total_recv
    overall_loss_pct = 0.0 if total_sent == 0 else (1.0 - (total_recv / total_sent)) * 100.0

    # Overall RTT and jitter stats
    if total_success > 0 and success_rtt_sum > 0:
        avg_rtt_all = success_rtt_sum / total_success
    else:
        avg_rtt_all = None

    if jitter_count_all > 0 and jitter_sum_all > 0:
        jitter_all = jitter_sum_all / jitter_count_all
    else:
        jitter_all = None

    # Add quality row to top table
    add_top_row(tr("QUALITY_LABEL"), quality_label)

    # Window row
    window_value = tr("WINDOW_VALUE", window_checks=window_size)
    add_top_row(tr("WINDOW_LABEL"), window_value)

    # Optional quality reason line
    if show_help and quality_reason and top_row_y < max_y:
        stdscr.addnstr(top_row_y, 2, f"- {quality_reason}"[: max_x - 2], max_x - 2)
        top_row_y += 1
    # --- Metrics table: window vs session ---
    # Leave one blank line between top info and metrics table
    metrics_start_y = top_row_y + 1

    if metrics_start_y < max_y:
        header = f"{'Metric':<8} {'Window':<36} | {'Session':<36}"
        stdscr.addnstr(metrics_start_y, 0, header[:max_x], max_x)

    row_y = metrics_start_y + 1

    def add_metric_row(label: str, win: str, all_: str):
        nonlocal row_y
        if row_y >= max_y:
            return
        line = f"{label:<8} {win:<36.36} | {all_:<36.36}"
        stdscr.addnstr(row_y, 0, line[:max_x], max_x)
        row_y += 1

    # Loss row
    if recent_count == 0:
        loss_win_str = tr("LOSS_WIN_VALUE_WAIT")
    else:
        loss_win_str = tr("LOSS_WIN_VALUE", loss_pct=recent_loss_pct, lost=recent_lost, count=recent_count)

    if total_sent == 0:
        loss_all_str = tr("LOSS_ALL_VALUE_WAIT")
    else:
        loss_all_str = tr("LOSS_ALL_VALUE", loss_pct=overall_loss_pct, lost=lost_overall, sent=total_sent)

    add_metric_row(tr("METRIC_LOSS_LABEL"), loss_win_str, loss_all_str)

    # Ping row
    if recent_avg_ping is None or recent_min_ping is None or recent_max_ping is None:
        ping_win_str = tr("PING_WIN_VALUE_WAIT")
    else:
        ping_win_str = tr(
            "PING_WIN_VALUE",
            min_ms=format_ping_value(recent_min_ping),
            avg_ms=format_ping_value(recent_avg_ping),
            max_ms=format_ping_value(recent_max_ping),
        )

    if avg_rtt_all is None or min_rtt_all is None or max_rtt_all is None:
        ping_all_str = tr("PING_ALL_VALUE_WAIT")
    else:
        ping_all_str = tr(
            "PING_ALL_VALUE",
            min_ms=format_ping_value(min_rtt_all),
            avg_ms=format_ping_value(avg_rtt_all),
            max_ms=format_ping_value(max_rtt_all),
        )

    add_metric_row(tr("METRIC_PING_LABEL"), ping_win_str, ping_all_str)

    # Jitter row
    if jitter_ms is None:
        jitter_win_str = tr("JITTER_WIN_VALUE_WAIT")
    else:
        jitter_win_str = tr("JITTER_WIN_VALUE", jitter_ms=jitter_ms)

    if jitter_all is None:
        jitter_all_str = tr("JITTER_ALL_VALUE_WAIT")
    else:
        jitter_all_str = tr("JITTER_ALL_VALUE", jitter_ms=jitter_all)

    add_metric_row(tr("METRIC_JITTER_LABEL"), jitter_win_str, jitter_all_str)

    table_bottom_y = row_y

    # Compute short- and long-term stats for alerts and admin view
    short_loss_pct, short_avg_ping, short_count, short_lost, short_min_ping, short_max_ping, _ = compute_recent_stats(
        ping_history, SHORT_WINDOW_SIZE
    )
    long_loss_pct, _, long_count, long_lost, _, _, _ = compute_recent_stats(
        ping_history, LONG_WINDOW_SIZE
    )

    # Admin-only extra stats: short vs long loss
    extras_start_y = table_bottom_y + 1
    next_y = extras_start_y

    if admin_mode and next_y < max_y:
        # Short term loss (e.g. last 10 checks)
        if short_count > 0:
            short_text = tr(
                "SHORT_LOSS_VALUE",
                seconds=SHORT_WINDOW_SIZE,
                loss_pct=short_loss_pct,
                lost=short_lost,
                count=short_count,
            )
        else:
            short_text = tr("SHORT_LOSS_WAIT")
        stdscr.addnstr(next_y, 0, short_text[:max_x], max_x)
        next_y += 1

        if next_y < max_y:
            if long_count > 0:
                long_text = tr(
                    "LONG_LOSS_VALUE",
                    seconds=min(long_count, LONG_WINDOW_SIZE),
                    loss_pct=long_loss_pct,
                    lost=long_lost,
                    count=long_count,
                )
            else:
                long_text = tr("LONG_LOSS_WAIT")
            stdscr.addnstr(next_y, 0, long_text[:max_x], max_x)
            next_y += 1

    # Spike indicator: detect significant short-term issues (shown to all users)
    # We look only at the short-term window here so alerts are tied to recent changes.
    alert_y = next_y if admin_mode else extras_start_y
    if alert_y < max_y:
        alert_text = ""
        # Consider a significant delay spike if max ping is much higher than average
        # over the short window and the deviation is large in absolute terms.
        if (
            short_count >= max(5, SHORT_WINDOW_SIZE // 2)
            and short_avg_ping is not None
            and short_max_ping is not None
            and short_max_ping > 3.0 * short_avg_ping
            and (short_max_ping - short_avg_ping) > 100.0
        ):
            alert_text = tr("ALERT_DELAY_SPIKE")
        # Consider packet loss significant only when it is clearly elevated.
        elif short_count >= max(5, SHORT_WINDOW_SIZE // 2) and short_loss_pct >= 10.0:
            alert_text = tr("ALERT_HIGH_LOSS")

        if alert_text:
            stdscr.addnstr(alert_y, 0, alert_text[:max_x], max_x)
            alert_y += 1

    # Traceroute section
    # Place traceroute header directly after the last alert/admin line (or directly after
    # the metrics table if there are none), to keep exactly one blank line after metrics.
    traceroute_header_y = alert_y
    if traceroute_header_y < max_y:
        if traceroute_running:
            header_base = tr("TRACE_RUNNING", host=target_host)
        else:
            if traceroute_error:
                header_base = tr("TRACE_FAILED", host=target_host)
            else:
                if show_traceroute_full:
                    header_base = tr("TRACE_FULL", host=target_host)
                else:
                    header_base = tr("TRACE_SUMMARY", host=target_host)

        # Append last-run time if available
        if last_traceroute_ts is not None:
            hhmm = time.strftime("%H:%M", time.localtime(last_traceroute_ts))
            last_run_short = tr("TRACE_LAST_RUN_SHORT", time=hhmm)
            tr_header = f"{header_base}  ({last_run_short})"
        else:
            tr_header = header_base

        stdscr.addnstr(traceroute_header_y, 0, tr_header[:max_x], max_x)

    # Optional final summary (after traceroute completes)
    summary_y = traceroute_header_y + 1
    if (not traceroute_running) and traceroute_summary and summary_y < max_y:
        stdscr.addnstr(summary_y, 0, traceroute_summary[:max_x], max_x)
        start_y = summary_y + 1
    else:
        start_y = summary_y

    if start_y < max_y:
        available_lines = max_y - start_y

        # Format traceroute as table lines
        table_lines = build_traceroute_table(traceroute_lines)

        if not table_lines:
            base_lines = []
        else:
            # Decide which lines to show: summary or full
            if show_traceroute_full or len(table_lines) <= 8:
                base_lines = table_lines
            else:
                base_lines = []
                # Keep header row
                base_lines.append(table_lines[0])
                # Show first few hops and last few hops
                body = table_lines[1:]
                if len(body) <= 6:
                    base_lines.extend(body)
                else:
                    base_lines.extend(body[:3])
                    base_lines.append(tr("TRACE_SUMMARY_HINT"))
                    base_lines.extend(body[-3:])

        # Apply scroll only when in full mode and there are more lines than we can show
        if show_traceroute_full and len(base_lines) > available_lines:
            max_offset = max(0, len(base_lines) - available_lines)
            offset = max(0, min(traceroute_scroll, max_offset))
        else:
            offset = 0

        visible_lines = base_lines[offset: offset + available_lines]

        for i, line in enumerate(visible_lines):
            stdscr.addnstr(start_y + i, 0, line[:max_x], max_x)

        # If there was an error, show it at the bottom
        if traceroute_error and available_lines > len(visible_lines):
            y = start_y + len(visible_lines)
            if y < max_y:
                err_text = tr("TRACE_ERROR_PREFIX", error=traceroute_error)
                stdscr.addnstr(y, 0, err_text[:max_x], max_x)

    stdscr.refresh()


def main(stdscr, admin_mode: bool, target_host: str):
    curses.curs_set(0)
    stdscr.nodelay(True)
    stdscr.timeout(200)  # getch timeout in ms

    state = MonitorState(target_host)

    # Start ping worker
    ping_thread = threading.Thread(
        target=ping_worker,
        args=(state, PING_INTERVAL_SECONDS),
        daemon=True,
    )
    ping_thread.start()

    # Start initial traceroute
    traceroute_thread = threading.Thread(
        target=traceroute_worker,
        args=(state,),
        daemon=True,
    )
    traceroute_thread.start()

    while True:
        draw_ui(stdscr, state, admin_mode=admin_mode)

        try:
            ch = stdscr.getch()
        except KeyboardInterrupt:
            ch = ord("q")

        if ch == -1:
            continue

        if ch in (ord("q"), ord("Q")):
            with state.lock:
                state.running = False
            break

        elif ch in (ord("p"), ord("P")):
            with state.lock:
                state.monitoring = False

        elif ch in (ord("r"), ord("R")):
            with state.lock:
                state.monitoring = True

        elif ch in (ord("t"), ord("T")):
            # Rerun traceroute if not already running
            with state.lock:
                already_running = state.traceroute_running
            if not already_running:
                t = threading.Thread(
                    target=traceroute_worker,
                    args=(state,),
                    daemon=True,
                )
                t.start()

        elif ch in (ord("h"), ord("H")):
            # Toggle help / description visibility
            with state.lock:
                state.show_help = not state.show_help

        elif ch in (ord("f"), ord("F")):
            # Toggle traceroute full/summary view
            with state.lock:
                state.show_traceroute_full = not state.show_traceroute_full
                state.traceroute_scroll = 0

        elif ch in (curses.KEY_UP,):
            # Scroll traceroute up when there is more content (affects full mode)
            with state.lock:
                state.traceroute_scroll = max(0, state.traceroute_scroll - 1)

        elif ch in (curses.KEY_DOWN,):
            # Scroll traceroute down when there is more content (affects full mode)
            with state.lock:
                state.traceroute_scroll += 1

        elif ch in (ord("l"), ord("L")):
            # Toggle language (e.g. en <-> id)
            cycle_language()

        elif ch in (ord("k"), ord("K")):
            # Toggle visibility of controls / keys guide
            with state.lock:
                state.show_controls = not state.show_controls

        # Increase / decrease time window (available to all users)
        elif ch == ord('+'):
            with state.lock:
                new_size = state.window_size + 10
                state.window_size = min(new_size, PING_HISTORY_LENGTH)

        elif ch == ord('-'):
            with state.lock:
                new_size = state.window_size - 10
                if new_size < MIN_WINDOW_SIZE:
                    new_size = MIN_WINDOW_SIZE
                state.window_size = new_size


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Simple network stability monitor (GMS)."
    )
    parser.add_argument(
        "--admin",
        action="store_true",
        help="enable extra controls for admins (time window adjustments).",
    )
    parser.add_argument(
        "--lang",
        default="en",
        choices=SUPPORTED_LANGS,
        help="language code (e.g. en, id)",
    )
    parser.add_argument(
        "--host",
        default=DEFAULT_TARGET_HOST,
        help="target host name or IP address to monitor (default: www.youtube.com)",
    )
    args = parser.parse_args()

    set_language(args.lang)
    curses.wrapper(lambda stdscr: main(stdscr, admin_mode=args.admin, target_host=args.host))
