"""Rip manifests, cleanup, and library repair."""

import json
import re
import shutil
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from ripper import state
from ripper.helpers import hash_file, sanitize_filename
from ripper.metadata import (
    download_artwork,
    tmdb_fetch_details,
    tmdb_fetch_tv_details,
    tmdb_get_season_episodes,
    tmdb_search,
    tmdb_search_tv,
    write_nfo,
    write_tv_nfo,
)

CONFIG = state.CONFIG


def write_rip_manifest(rip_dir, files_info):
    """Write a manifest file alongside rips for integrity verification."""
    manifest_path = Path(rip_dir) / "rip_manifest.json"
    entries = []
    for fpath, ep_num in files_info:
        fpath = Path(fpath)
        if not fpath.exists():
            continue
        state.log.info(f"  Hashing {fpath.name}...")
        h = hash_file(fpath)
        entries.append({
            "filename": fpath.name,
            "size_bytes": fpath.stat().st_size,
            "md5": h,
            "episode": ep_num,
            "ripped_at": datetime.now().isoformat(),
        })

    manifest = {
        "rip_dir": str(rip_dir),
        "created": datetime.now().isoformat(),
        "files": entries,
    }

    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    state.log.info(f"  Manifest written: {manifest_path.name} ({len(entries)} files)")
    return manifest_path


def verify_rip_manifest(rip_dir):
    """Verify ripped files against their manifest."""
    manifest_path = Path(rip_dir) / "rip_manifest.json"
    if not manifest_path.exists():
        state.log.warning(f"  No manifest found in {rip_dir}")
        return None

    with open(manifest_path) as f:
        manifest = json.load(f)

    valid = []
    for entry in manifest.get("files", []):
        fpath = Path(rip_dir) / entry["filename"]
        if not fpath.exists():
            state.log.warning(f"  Missing: {entry['filename']}")
            continue
        if fpath.stat().st_size != entry["size_bytes"]:
            state.log.warning(f"  Size mismatch: {entry['filename']}")
            continue
        valid.append((fpath, entry.get("episode")))
        state.log.info(f"  Verified: {entry['filename']} ({entry['size_bytes'] / (1024**3):.1f} GB)")

    return valid


def cleanup_rips(rip_dir=None):
    """Remove raw rips."""
    if rip_dir:
        rip_dir = Path(rip_dir)
        if rip_dir.exists():
            size_gb = sum(f.stat().st_size for f in rip_dir.rglob("*") if f.is_file()) / (1024 ** 3)
            state.log.info(f"Cleaning up {rip_dir.name} ({size_gb:.1f} GB)...")
            shutil.rmtree(rip_dir, ignore_errors=True)
            state.log.info("  Done.")
    else:
        rips_base = Path(CONFIG["rip_dir"])
        if not rips_base.exists():
            state.log.info("No rips directory found.")
            return
        total = 0
        for d in sorted(rips_base.iterdir()):
            if d.is_dir():
                size_gb = sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) / (1024 ** 3)
                state.log.info(f"  {d.name}: {size_gb:.1f} GB")
                total += size_gb
        if total == 0:
            state.log.info("  No rips to clean up.")
            return
        state.log.info(f"\n  Total: {total:.1f} GB")
        if sys.stdin.isatty():
            confirm = input("  Delete all rips? [y/N]: ").strip().lower()
            if confirm != 'y':
                state.log.info("  Skipped.")
                return
        for d in rips_base.iterdir():
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
        state.log.info("  All rips cleaned up.")


# ============================================================================
# LIBRARY REPAIR
# ============================================================================

def _parse_tmdb_id_from_nfo(nfo_path):
    """Extract the TMDb ID from a .nfo file."""
    try:
        tree = ET.parse(nfo_path)
        root = tree.getroot()
        for uid in root.findall("uniqueid"):
            if uid.get("type") == "tmdb" and uid.text:
                return int(uid.text)
    except (ET.ParseError, ValueError, OSError):
        pass
    return None


def _parse_folder_title_year(folder_name):
    """Extract title and year from 'The Matrix (1999)'. Returns (title, year|None)."""
    m = re.match(r"^(.+?)\s*\((\d{4})\)\s*$", folder_name)
    if m:
        return m.group(1).strip(), m.group(2)
    return folder_name, None


def repair_library():
    """Scan library for missing artwork/NFO and re-fetch from TMDb."""
    movies_dir = Path(CONFIG["encode_dir"])
    shows_dir = Path(CONFIG["tv_encode_dir"])

    stats = {"scanned": 0, "repaired": 0, "failed": 0}

    state.console.print("\n[bold]Library Repair[/bold] — scanning for missing artwork & metadata\n")

    # ---- Movies ----
    if movies_dir.exists():
        movie_folders = sorted([d for d in movies_dir.iterdir() if d.is_dir()])
        if movie_folders:
            state.console.print(f"[bold]Movies[/bold] ({movies_dir})")
        for folder in movie_folders:
            stats["scanned"] += 1
            has_nfo = any(folder.glob("*.nfo"))
            has_poster = (folder / "poster.jpg").exists()
            has_fanart = (folder / "fanart.jpg").exists()

            if has_nfo and has_poster and has_fanart:
                state.console.print(f"  [green]OK[/green]  {folder.name}")
                continue

            missing = []
            if not has_nfo:
                missing.append("NFO")
            if not has_poster:
                missing.append("poster")
            if not has_fanart:
                missing.append("fanart")

            state.console.print(f"  [yellow]FIX[/yellow] {folder.name}  (missing: {', '.join(missing)})")

            meta = None
            if has_nfo:
                nfo_files = list(folder.glob("*.nfo"))
                for nf in nfo_files:
                    tmdb_id = _parse_tmdb_id_from_nfo(nf)
                    if tmdb_id:
                        meta = {"tmdb_id": tmdb_id}
                        meta = tmdb_fetch_details(meta)
                        break

            if not meta or not meta.get("tmdb_id"):
                title, year = _parse_folder_title_year(folder.name)
                query = f"{title} {year}" if year else title
                meta = tmdb_search(query)

            if not meta:
                state.console.print("        [red]FAIL[/red] Could not find on TMDb")
                stats["failed"] += 1
                continue

            repaired = False
            if not has_poster or not has_fanart:
                download_artwork(meta, folder)
                repaired = True
            if not has_nfo:
                folder_label = sanitize_filename(
                    f"{meta.get('title', folder.name)} ({meta.get('year', '')})"
                    if meta.get("year") else meta.get("title", folder.name)
                )
                write_nfo(meta, folder, folder_label)
                repaired = True

            if repaired:
                stats["repaired"] += 1
    else:
        state.console.print(f"[dim]Movies directory not found: {movies_dir}[/dim]")

    # ---- TV Shows ----
    if shows_dir.exists():
        show_folders = sorted([d for d in shows_dir.iterdir() if d.is_dir()])
        if show_folders:
            state.console.print(f"\n[bold]TV Shows[/bold] ({shows_dir})")
        for folder in show_folders:
            stats["scanned"] += 1
            has_nfo = (folder / "tvshow.nfo").exists()
            has_poster = (folder / "poster.jpg").exists()
            has_fanart = (folder / "fanart.jpg").exists()

            show_missing = []
            if not has_nfo:
                show_missing.append("NFO")
            if not has_poster:
                show_missing.append("poster")
            if not has_fanart:
                show_missing.append("fanart")

            ep_missing_thumbs = []
            season_dirs = sorted([d for d in folder.iterdir()
                                  if d.is_dir() and d.name.startswith("Season")])
            for season_dir in season_dirs:
                for mkv in sorted(season_dir.glob("*.mkv")):
                    thumb = mkv.with_name(mkv.stem + "-thumb.jpg")
                    if not thumb.exists():
                        ep_missing_thumbs.append(mkv)

            if not show_missing and not ep_missing_thumbs:
                state.console.print(f"  [green]OK[/green]  {folder.name}")
                continue

            if show_missing:
                state.console.print(f"  [yellow]FIX[/yellow] {folder.name}  (missing: {', '.join(show_missing)})")
            if ep_missing_thumbs:
                state.console.print(f"  [yellow]FIX[/yellow] {folder.name}  ({len(ep_missing_thumbs)} episode thumbnail(s) missing)")

            meta = None
            if has_nfo:
                tmdb_id = _parse_tmdb_id_from_nfo(folder / "tvshow.nfo")
                if tmdb_id:
                    meta = {"tmdb_id": tmdb_id, "media_type": "tv"}
                    meta = tmdb_fetch_tv_details(meta)

            if not meta or not meta.get("tmdb_id"):
                title, year = _parse_folder_title_year(folder.name)
                meta = tmdb_search_tv(title)

            if not meta:
                state.console.print("        [red]FAIL[/red] Could not find on TMDb")
                stats["failed"] += 1
                continue

            repaired = False

            if not has_poster or not has_fanart:
                download_artwork(meta, folder)
                repaired = True
            if not has_nfo:
                title, _ = _parse_folder_title_year(folder.name)
                write_tv_nfo(meta, folder, meta.get("title", title), 1)
                repaired = True

            if ep_missing_thumbs and meta.get("tmdb_id"):
                seasons_needed = set()
                for mkv in ep_missing_thumbs:
                    m = re.search(r"S(\d+)E\d+", mkv.stem)
                    if m:
                        seasons_needed.add(int(m.group(1)))

                ep_stills = {}
                for sn in sorted(seasons_needed):
                    episodes_info = tmdb_get_season_episodes(meta["tmdb_id"], sn)
                    for ei in episodes_info:
                        sp = ei.get("still_path")
                        if sp:
                            ep_stills[(sn, ei["episode_number"])] = sp

                for mkv in ep_missing_thumbs:
                    m = re.search(r"S(\d+)E(\d+)", mkv.stem)
                    if not m:
                        continue
                    sn, en = int(m.group(1)), int(m.group(2))
                    still = ep_stills.get((sn, en))
                    if still:
                        thumb_path = mkv.with_name(mkv.stem + "-thumb.jpg")
                        url = f"https://image.tmdb.org/t/p/original{still}"
                        try:
                            urllib.request.urlretrieve(url, str(thumb_path))
                            state.console.print(f"        [green]DL[/green]  {thumb_path.name}")
                            repaired = True
                        except (urllib.error.URLError, OSError) as e:
                            state.console.print(f"        [red]FAIL[/red] {mkv.stem} thumb: {e}")
                    else:
                        state.console.print(f"        [dim]SKIP[/dim] {mkv.stem} (no still on TMDb)")

            if repaired:
                stats["repaired"] += 1
    else:
        state.console.print(f"[dim]Shows directory not found: {shows_dir}[/dim]")

    state.console.print(f"\n[bold]Summary:[/bold] {stats['scanned']} scanned, "
                        f"{stats['repaired']} repaired, {stats['failed']} failed\n")
