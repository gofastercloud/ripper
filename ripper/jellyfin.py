"""Jellyfin media server integration."""

import json
import urllib.error
import urllib.request

from ripper import state

CONFIG = state.CONFIG


def jellyfin_scan_library(quiet=False):
    """Trigger a Jellyfin library scan so new content appears immediately."""
    api_key = CONFIG["jellyfin_api_key"]
    base_url = CONFIG["jellyfin_url"]

    if not api_key:
        if not quiet:
            state.log.info("No Jellyfin API key configured — skipping library scan.")
            state.log.info("Set JELLYFIN_API_KEY to enable auto-scan after rips.")
        return False

    url = f"{base_url}/Library/Refresh"
    if not quiet:
        state.log.info("Triggering Jellyfin library scan...")

    try:
        req = urllib.request.Request(
            url,
            method="POST",
            headers={
                "Authorization": f'MediaBrowser Token="{api_key}"',
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status in (200, 204):
                if quiet:
                    state.log.info("  [ENCODE] Jellyfin scan triggered")
                else:
                    state.log.info("  Jellyfin library scan triggered")
                return True
    except (urllib.error.URLError, OSError) as e:
        if not quiet:
            state.log.warning(f"  Jellyfin scan failed: {e}")
            state.log.warning("  Is Jellyfin running? Check http://localhost:8096")
        return False

    return False


def jellyfin_check_status():
    """Check if Jellyfin is running and reachable."""
    base_url = CONFIG["jellyfin_url"]
    try:
        req = urllib.request.Request(f"{base_url}/System/Info/Public")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode())
            version = data.get("Version", "unknown")
            server_name = data.get("ServerName", "unknown")
            state.log.info(f"  Jellyfin {version} ({server_name}) is running")
            return True
    except (urllib.error.URLError, OSError):
        return False
