"""Rich-based terminal UI and logging setup."""

import io
import logging
import os
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from rich import box
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ripper import state

CONFIG = state.CONFIG

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None


# ============================================================================
# POSTER ASCII ART
# ============================================================================

_poster_cache = {}


def poster_to_ascii(poster_path, width=None, height=None):
    """
    Download a TMDb poster and convert to Rich Text using half-block
    characters for double vertical resolution with true-color output.
    """
    if not poster_path or PILImage is None:
        return None

    if poster_path in _poster_cache:
        return _poster_cache[poster_path]

    try:
        if width is None or height is None:
            term_cols = os.get_terminal_size(0).columns if sys.stdout.isatty() else 140
            term_rows = os.get_terminal_size(0).lines if sys.stdout.isatty() else 40
            panel_width = (term_cols // 3) - 4
            panel_height = term_rows - 15
            width = max(panel_width, 20)
            height = max(panel_height * 2, 30)

        url = f"https://image.tmdb.org/t/p/w342{poster_path}"
        req = urllib.request.Request(url, headers={"User-Agent": "ripper/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            img_data = resp.read()

        img = PILImage.open(io.BytesIO(img_data)).convert("RGB")
        img = img.resize((width, height), PILImage.LANCZOS)

        pixels = img.load()
        lines = []
        for row in range(0, height - 1, 2):
            line_parts = []
            for col in range(width):
                top_r, top_g, top_b = pixels[col, row]
                bot_r, bot_g, bot_b = pixels[col, row + 1]
                line_parts.append(
                    f"[rgb({top_r},{top_g},{top_b}) on rgb({bot_r},{bot_g},{bot_b})]▀[/]"
                )
            lines.append("".join(line_parts))

        result = Text.from_markup("\n".join(lines))
        _poster_cache[poster_path] = result
        return result

    except Exception:
        _poster_cache[poster_path] = None
        return None


# ============================================================================
# TUI
# ============================================================================

class RipperTUI:
    """Live terminal UI using rich."""

    def __init__(self):
        self.live = None
        self.enabled = False

        self.title = ""
        self.year = ""
        self.media_type = "movie"
        self.source_format = ""
        self.encoder_mode = ""

        self.rip_pct = 0.0
        self.rip_task = ""
        self.encode_pct = 0.0
        self.encode_eta = ""
        self.encode_task = ""

        self.episodes = []
        self.current_rip_ep = None
        self.current_encode_ep = None

        self.poster_art = None

        self.disc_start_time = None
        self._disc_eta_str = ""
        self._last_eta_update = 0.0

        self._log_lines = []
        self._max_log = 8
        self._lock = threading.Lock()

    def start(self):
        """Start the live display and suppress the stdout log handler."""
        self.enabled = True
        if state._stdout_handler and state.log:
            state.log.root.removeHandler(state._stdout_handler)
        self.live = Live(
            self._render(),
            console=state.console,
            refresh_per_second=4,
            transient=False,
        )
        self.live.start()

    def stop(self):
        """Stop the live display and restore the stdout log handler."""
        if self.live:
            self.live.stop()
            self.live = None
        self.enabled = False
        if state._stdout_handler and state.log:
            state.log.root.addHandler(state._stdout_handler)

    def log(self, msg):
        """Add a message to the scrolling log area."""
        with self._lock:
            self._log_lines.append(msg)
            if len(self._log_lines) > self._max_log:
                self._log_lines = self._log_lines[-self._max_log:]
        self._refresh()

    def set_metadata(self, title, year=None, media_type="movie",
                     source_format="", encoder_mode=""):
        self.title = title
        self.year = year or ""
        self.media_type = media_type
        self.source_format = source_format
        self.encoder_mode = encoder_mode
        self._refresh()

    def set_poster(self, poster_renderable):
        self.poster_art = poster_renderable
        self._refresh()

    def start_disc_timer(self):
        self.disc_start_time = time.monotonic()
        self._disc_eta_str = ""
        self._last_eta_update = 0.0

    def _disc_overall_pct(self):
        if self.episodes:
            n = len(self.episodes)
            if n == 0:
                return 0.0
            completed = 0.0
            for ep in self.episodes:
                if ep["status"] == "done":
                    completed += 2.0
                elif ep["status"] == "failed":
                    completed += 2.0
                elif ep["status"] == "encoding":
                    completed += 1.0
                    completed += self.encode_pct / 100.0
                elif ep["status"] == "ripped":
                    completed += 1.0
                elif ep["status"] == "ripping":
                    completed += self.rip_pct / 100.0
            return (completed / (n * 2.0)) * 100.0
        else:
            return self.rip_pct * 0.5 + self.encode_pct * 0.5

    def _format_duration(self, seconds):
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            m, s = divmod(int(seconds), 60)
            return f"{m}m {s:02d}s"
        else:
            h, rem = divmod(int(seconds), 3600)
            m = rem // 60
            return f"{h}h {m:02d}m"

    def _get_disc_eta(self):
        now = time.monotonic()
        if not self.disc_start_time:
            return ""
        elapsed = now - self.disc_start_time
        if elapsed < 5:
            return ""

        if now - self._last_eta_update >= 30 or not self._disc_eta_str:
            pct = self._disc_overall_pct()
            if pct > 0.5:
                remaining = elapsed * (100.0 - pct) / pct
                self._disc_eta_str = self._format_duration(remaining)
            else:
                self._disc_eta_str = "calculating..."
            self._last_eta_update = now
        return self._disc_eta_str

    def set_episodes(self, episodes):
        self.episodes = [
            {"num": num, "name": name, "status": "pending"}
            for num, name in episodes
        ]
        self._refresh()

    def set_episode_status(self, ep_num, status_val):
        for ep in self.episodes:
            if ep["num"] == ep_num:
                ep["status"] = status_val
                break
        self._refresh()

    def update_rip(self, pct, task=""):
        self.rip_pct = pct
        if task:
            self.rip_task = task
        self._refresh()

    def update_encode(self, pct, eta="", task=""):
        self.encode_pct = pct
        self.encode_eta = eta
        if task:
            self.encode_task = task
        self._refresh()

    def _refresh(self):
        if self.live and self.enabled:
            try:
                self.live.update(self._render())
            except Exception:
                pass

    def _render(self):
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="body"),
            Layout(name="log", size=min(self._max_log + 2, 10)),
        )

        year_str = f" ({self.year})" if self.year else ""
        type_badge = f"[bold cyan]{self.media_type.upper()}[/]"
        source_badge = f"[dim]{self.source_format}[/]" if self.source_format else ""
        enc_badge = f"[dim]{self.encoder_mode}[/]" if self.encoder_mode else ""
        header_text = f" {type_badge}  [bold white]{self.title}{year_str}[/]  {source_badge}  {enc_badge}"
        layout["header"].update(Panel(header_text, box=box.HEAVY, style="blue"))

        if self.poster_art:
            poster_panel = Panel(
                self.poster_art, title="[dim]Poster[/]",
                box=box.ROUNDED, style="dim",
            )
            if self.episodes:
                layout["body"].split_row(
                    Layout(name="poster", ratio=1),
                    Layout(name="progress", ratio=1),
                    Layout(name="queue", ratio=1),
                )
                layout["body"]["poster"].update(poster_panel)
                layout["body"]["progress"].update(self._render_progress())
                layout["body"]["queue"].update(self._render_queue())
            else:
                layout["body"].split_row(
                    Layout(name="poster", ratio=1),
                    Layout(name="progress", ratio=2),
                )
                layout["body"]["poster"].update(poster_panel)
                layout["body"]["progress"].update(self._render_progress())
        elif self.episodes:
            layout["body"].split_row(
                Layout(name="progress", ratio=1),
                Layout(name="queue", ratio=1),
            )
            layout["body"]["progress"].update(self._render_progress())
            layout["body"]["queue"].update(self._render_queue())
        else:
            layout["body"].update(self._render_progress())

        with self._lock:
            log_text = "\n".join(self._log_lines[-self._max_log:]) if self._log_lines else "[dim]Waiting...[/]"
        layout["log"].update(Panel(log_text, title="[dim]Log[/]", box=box.ROUNDED, style="dim"))

        return layout

    def _render_progress(self):
        table = Table(box=None, show_header=False, expand=True, padding=(1, 2))
        table.add_column(ratio=1)

        if self.disc_start_time:
            elapsed = time.monotonic() - self.disc_start_time
            elapsed_str = self._format_duration(elapsed)
            disc_eta = self._get_disc_eta()
            overall_pct = self._disc_overall_pct()
            if disc_eta:
                table.add_row(f"[bold magenta]DISC[/]  [bold]{overall_pct:4.0f}%[/]  elapsed {elapsed_str}  ·  [bold]~{disc_eta} remaining[/]")
            else:
                table.add_row(f"[bold magenta]DISC[/]  [bold]{overall_pct:4.0f}%[/]  elapsed {elapsed_str}")
            table.add_row("")

        rip_bar = self._bar_string(self.rip_pct, "green")
        rip_label = self.rip_task or "Rip"
        table.add_row(f"[bold]RIP[/]  {rip_label}")
        table.add_row(f"  {rip_bar}  [bold]{self.rip_pct:5.1f}%[/]")
        table.add_row("")

        enc_bar = self._bar_string(self.encode_pct, "yellow")
        enc_label = self.encode_task or "Encode"
        eta_str = f"  ETA {self.encode_eta}" if self.encode_eta else ""
        table.add_row(f"[bold]ENC[/]  {enc_label}")
        table.add_row(f"  {enc_bar}  [bold]{self.encode_pct:5.1f}%[/]{eta_str}")

        return Panel(table, title="[bold]Progress[/]", box=box.ROUNDED)

    def _render_queue(self):
        table = Table(box=box.SIMPLE, expand=True, show_header=True)
        table.add_column("#", style="dim", width=4)
        table.add_column("Episode", ratio=1)
        table.add_column("Status", width=10, justify="right")

        status_styles = {
            "pending":  "[dim]waiting[/]",
            "ripping":  "[bold green]ripping[/]",
            "ripped":   "[green]ripped[/]",
            "encoding": "[bold yellow]encoding[/]",
            "done":     "[bold blue]done[/]",
            "failed":   "[bold red]FAILED[/]",
        }

        for ep in self.episodes:
            num_str = f"E{ep['num']:02d}"
            name = ep["name"] or f"Episode {ep['num']}"
            status_val = status_styles.get(ep["status"], ep["status"])
            table.add_row(num_str, name, status_val)

        return Panel(table, title="[bold]Episodes[/]", box=box.ROUNDED)

    @staticmethod
    def _bar_string(pct, color, width=30):
        filled = int(width * pct / 100)
        empty = width - filled
        return f"[{color}]{'━' * filled}[/][dim]{'─' * empty}[/]"


# ============================================================================
# LOGGING
# ============================================================================

class _TUILogHandler(logging.Handler):
    """Routes log messages to the TUI when active."""
    def emit(self, record):
        if state.tui and state.tui.enabled:
            msg = record.getMessage().strip()
            if msg:
                state.tui.log(msg)


def setup_logging():
    """Set up dual logging: file + stdout (with TUI handler)."""
    state._stdout_handler = logging.StreamHandler(sys.stdout)
    handlers = [state._stdout_handler]

    log_dir = Path(CONFIG["log_dir"])
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"pipeline_{datetime.now():%Y%m%d_%H%M%S}.log"
        handlers.append(logging.FileHandler(log_file))
    except OSError:
        pass

    handlers.append(_TUILogHandler())

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
    )
    return logging.getLogger("bluray_pipeline")
