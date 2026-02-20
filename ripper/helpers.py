"""Subprocess runners, filesystem helpers, and API key validation."""

import hashlib
import json
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from ripper import state

CONFIG = state.CONFIG


def run_cmd(cmd, timeout=None, capture=True):
    """Run a shell command and return the result."""
    if state.is_shutdown_requested():
        return None
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            timeout=timeout,
        )
        return result
    except subprocess.TimeoutExpired:
        state.log.error(f"Command timed out: {' '.join(cmd[:3])}...")
        return None
    except FileNotFoundError:
        state.log.error(f"Command not found: {cmd[0]}")
        return None


def run_cmd_progress(cmd, timeout=None, progress_handler=None):
    """
    Run a command with real-time stdout streaming for progress reporting.
    Thread-safe: multiple calls can run concurrently (e.g. rip + encode).
    progress_handler: callable(line) that processes each stdout line.
    Returns (returncode, stderr_text).
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        with state._active_processes_lock:
            state._active_processes.append(proc)

        stderr_chunks = []

        def _drain_stderr():
            for line in proc.stderr:
                stderr_chunks.append(line)

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        start = time.time()

        for line in proc.stdout:
            if state.is_shutdown_requested():
                proc.terminate()
                proc.wait(timeout=10)
                return None, "Cancelled by user"
            if timeout and (time.time() - start) > timeout:
                proc.kill()
                state.log.error(f"Command timed out after {timeout}s")
                return None, ""
            line = line.rstrip()
            if progress_handler:
                progress_handler(line)

        proc.wait()
        stderr_thread.join(timeout=5)
        with state._active_processes_lock:
            if proc in state._active_processes:
                state._active_processes.remove(proc)
        stderr_text = "".join(stderr_chunks)
        return proc.returncode, stderr_text

    except FileNotFoundError:
        state.log.error(f"Command not found: {cmd[0]}")
        return None, ""


def sanitize_filename(name):
    """Clean a string for use as a filename."""
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name


def disc_is_inserted():
    """Check if a disc is currently in the drive."""
    result = run_cmd(["drutil", "status"], timeout=10)
    if result and result.returncode == 0:
        return "No Media Inserted" not in result.stdout
    return False


def eject_disc():
    """Eject the disc. Unmount any auto-mounted data partition first, then eject."""
    state.log.info("Ejecting disc...")

    try:
        result = run_cmd(["diskutil", "list"], timeout=10)
        if result and result.stdout:
            for line in result.stdout.splitlines():
                if line.strip().startswith("/dev/disk") and ("BD-ROM" in line or "DVD" in line or "CD" in line or "optical" in line.lower()):
                    dev = line.strip().split()[0]
                    state.log.info(f"  Unmounting {dev}...")
                    run_cmd(["diskutil", "unmountDisk", dev], timeout=10)
    except Exception as e:
        state.log.warning(f"  diskutil unmount attempt: {e}")

    run_cmd(["drutil", "eject"], timeout=10)


def ensure_dirs():
    """Create output directories if they don't exist."""
    for key in ["rip_dir", "encode_dir", "tv_encode_dir", "log_dir"]:
        Path(CONFIG[key]).mkdir(parents=True, exist_ok=True)


def check_external_drive():
    """Verify the external drive is mounted."""
    base = Path(CONFIG["output_base"])
    if not base.exists():
        state.log.error(f"External drive not found at {base}")
        state.log.error("Make sure EXTNVMESSD1 is connected and mounted.")
        return False
    free_gb = shutil.disk_usage(base).free / (1024 ** 3)
    state.log.info(f"External drive mounted. Free space: {free_gb:.1f} GB")
    if free_gb < 80:
        state.log.warning("Low disk space! A 4K rip + encode can need 60-100 GB temporarily.")
    return True


def validate_api_keys():
    """
    Validate that required API keys are set and working.
    Returns True if all keys are valid, False otherwise.
    """
    ok = True

    # --- TMDb ---
    tmdb_key = CONFIG["tmdb_api_key"]
    if not tmdb_key:
        state.log.error("TMDB_API_KEY is not set.")
        state.log.error("  1. Sign up at themoviedb.org")
        state.log.error("  2. Go to Settings > API > copy your v3 key")
        state.log.error("  3. export TMDB_API_KEY=\"your_key\"")
        ok = False
    else:
        try:
            url = f"https://api.themoviedb.org/3/configuration?api_key={tmdb_key}"
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    state.log.info("TMDb API key: valid")
                else:
                    state.log.error(f"TMDb API key: invalid (HTTP {resp.status})")
                    ok = False
        except urllib.error.HTTPError as e:
            if e.code == 401:
                state.log.error("TMDb API key: invalid (unauthorized). Check your key.")
            else:
                state.log.error(f"TMDb API key: check failed (HTTP {e.code})")
            ok = False
        except (urllib.error.URLError, OSError) as e:
            state.log.warning(f"TMDb API key: could not verify (network error: {e})")
            state.log.warning("  Continuing anyway — metadata lookup may fail.")

    # --- Jellyfin ---
    jf_key = CONFIG["jellyfin_api_key"]
    jf_url = CONFIG["jellyfin_url"]
    if not jf_key:
        state.log.warning("JELLYFIN_API_KEY is not set — library auto-scan disabled.")
        state.log.warning("  Dashboard > API Keys > create one, then:")
        state.log.warning("  export JELLYFIN_API_KEY=\"your_key\"")
    else:
        try:
            url = f"{jf_url}/System/Info"
            req = urllib.request.Request(
                url,
                headers={"Authorization": f'MediaBrowser Token="{jf_key}"'},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode())
                    server = data.get("ServerName", "Jellyfin")
                    version = data.get("Version", "?")
                    state.log.info(f"Jellyfin API key: valid ({server} v{version})")
                else:
                    state.log.warning(f"Jellyfin API key: unexpected response (HTTP {resp.status})")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                state.log.error("Jellyfin API key: invalid (unauthorized). Check your key.")
                state.log.error("  Dashboard > API Keys to verify.")
            else:
                state.log.warning(f"Jellyfin API key: check failed (HTTP {e.code})")
        except (urllib.error.URLError, OSError):
            state.log.warning(f"Jellyfin not reachable at {jf_url} — is it running?")
            state.log.warning("  Library scan will be skipped.")

    return ok


def hash_file(filepath, algorithm="md5"):
    """Compute hash of a file. Uses MD5 by default (fast, good enough for integrity)."""
    h = hashlib.new(algorithm)
    with open(filepath, "rb") as f:
        while True:
            chunk = f.read(8 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()
