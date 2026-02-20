"""TMDb search/fetch, disc label parsing, NFO writing, and artwork download."""

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ripper import state

CONFIG = state.CONFIG


# ============================================================================
# DISC LABEL DETECTION
# ============================================================================

def extract_disc_label():
    """Extract the disc volume label from MakeMKV's robot-mode output."""
    from ripper.helpers import run_cmd

    makemkv = CONFIG["makemkv_bin"]
    state.log.info("Reading disc volume label...")

    result = run_cmd([makemkv, "-r", "info", "disc:0"], timeout=120)
    if result is None:
        return None

    disc_name = None
    cinfo_name = None

    for line in result.stdout.splitlines():
        if line.startswith("CINFO:2,"):
            match = re.match(r'CINFO:2,\d+,"(.*)"', line)
            if match:
                cinfo_name = match.group(1)
        if line.startswith("CINFO:32,"):
            match = re.match(r'CINFO:32,\d+,"(.*)"', line)
            if match:
                disc_name = match.group(1)

    label = disc_name or cinfo_name
    if label:
        state.log.info(f"  Disc label: {label}")
    else:
        state.log.warning("  Could not read disc label.")
    return label


def clean_disc_label(raw_label):
    """
    Convert a raw disc label like 'BLADE_RUNNER_2049_4KUHD' into a search-
    friendly string like 'Blade Runner 2049'.

    Returns (cleaned_label, season_num_or_None, disc_num_or_None).
    """
    if not raw_label:
        return None, None, None

    label = raw_label.upper()

    season_num = None
    disc_num = None

    season_patterns = [
        r'_?S(\d{1,2})_?(?:D|DISC|DISK)?_?(\d+)?',
        r'_?SEASON_?(\d{1,2})_?(?:D|DISC|DISK)?_?(\d+)?',
        r'_?COMPLETE_?SERIES',
        r'_?SERIES_?(\d{1,2})',
    ]
    for pattern in season_patterns:
        match = re.search(pattern, label, re.IGNORECASE)
        if match:
            groups = match.groups()
            if groups[0] and groups[0].isdigit():
                season_num = int(groups[0])
            if len(groups) > 1 and groups[1] and groups[1].isdigit():
                disc_num = int(groups[1])
            label = re.sub(pattern, '', label, flags=re.IGNORECASE)
            break

    if disc_num is None:
        disc_match = re.search(r'_?(?:DISC|DISK|D)_?(\d+)', label, re.IGNORECASE)
        if disc_match:
            disc_num = int(disc_match.group(1))
            label = re.sub(r'_?(?:DISC|DISK|D)_?\d+', '', label, flags=re.IGNORECASE)

    strip_patterns = [
        r'[_\s]4K[_\s]?UHD',
        r'[_\s]UHD\b',
        r'[_\s]BLU[_\s]?RAY',
        r'[_\s]BLURAY',
        r'[_\s]BD[_\s]?\d*$',
        r'[_\s]FPL[_\s]\d+',
        r'[_\s]CEE?$',
        r'[_\s]USA?$',
        r'[_\s]GBR?$',
        r'[_\s]EUR?$',
        r'[_\s]REG[_\s]?[A-C]',
        r'[_\s]DVD\d?$',
        r'[_\s]COMPLETE[_\s]?SERIES',
    ]
    for pattern in strip_patterns:
        label = re.sub(pattern, '', label, flags=re.IGNORECASE)

    stripped = re.sub(r'\d+$', '', label).strip('_').strip()
    if stripped and len(stripped) >= 3:
        trailing = label[len(stripped):].strip('_').strip()
        if trailing and not re.match(r'^(19|20)\d{2}$', trailing):
            label = stripped

    label = label.replace('_', ' ')
    label = re.sub(r'\s+', ' ', label).strip()
    label = label.title()

    state.log.info(f"  Cleaned label: {label}")
    if season_num is not None:
        state.log.info(f"  Detected season: {season_num}" + (f", disc {disc_num}" if disc_num else ""))

    return label, season_num, disc_num


# ============================================================================
# TMDb API
# ============================================================================

def tmdb_search(query):
    """Search TMDb for a movie. Returns metadata dict or None."""
    api_key = CONFIG["tmdb_api_key"]
    if not api_key:
        state.log.warning("No TMDb API key set. Skipping metadata lookup.")
        state.log.warning("Get a free key: https://www.themoviedb.org/settings/api")
        state.log.warning("Then: export TMDB_API_KEY=\"your_key\"")
        return None

    encoded_query = urllib.parse.quote(query)
    url = (
        f"https://api.themoviedb.org/3/search/movie"
        f"?api_key={api_key}&query={encoded_query}&include_adult=false"
    )

    state.log.info(f"  Searching TMDb for: {query}")

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        state.log.warning(f"  TMDb search failed: {e}")
        return None

    results = data.get("results", [])
    if not results:
        state.log.warning(f"  No TMDb results for '{query}'")
        return None

    hit = results[0]
    release_date = hit.get("release_date", "")
    year = release_date[:4] if release_date else None

    metadata = {
        "title": hit.get("title", query),
        "year": year,
        "overview": hit.get("overview", ""),
        "tmdb_id": hit.get("id"),
        "poster_path": hit.get("poster_path"),
        "original_title": hit.get("original_title"),
        "vote_average": hit.get("vote_average"),
        "genres": [],
        "directors": [],
        "cast": [],
        "studio": None,
        "tagline": None,
        "runtime": None,
        "certification": None,
    }

    state.log.info(f"  TMDb match: {metadata['title']} ({metadata['year']})")
    if len(results) > 1:
        state.log.info(f"  ({len(results) - 1} other candidates — use --title to override)")
        for alt in results[1:4]:
            alt_year = alt.get("release_date", "")[:4]
            state.log.info(f"    - {alt.get('title')} ({alt_year})")

    metadata = tmdb_fetch_details(metadata)
    return metadata


def tmdb_dual_search(query):
    """Search TMDb for both movies and TV shows. Returns (metadata, type_str)."""
    api_key = CONFIG["tmdb_api_key"]
    if not api_key:
        return None, "movie"

    encoded_query = urllib.parse.quote(query)
    url = (
        f"https://api.themoviedb.org/3/search/multi"
        f"?api_key={api_key}&query={encoded_query}&include_adult=false"
    )

    state.log.info(f"  Searching TMDb (movie + TV) for: {query}")

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        state.log.warning(f"  TMDb multi-search failed, trying movie search: {e}")
        meta = tmdb_search(query)
        return meta, "movie"

    results = [r for r in data.get("results", []) if r.get("media_type") in ("movie", "tv")]

    if not results:
        state.log.warning(f"  No TMDb results for '{query}'")
        return None, "movie"

    hit = results[0]
    detected_type = hit.get("media_type", "movie")

    if detected_type == "tv":
        title = hit.get("name", query)
        year = (hit.get("first_air_date") or "")[:4] or None
    else:
        title = hit.get("title", query)
        year = (hit.get("release_date") or "")[:4] or None

    state.log.info(f"  Best match [{detected_type.upper()}]: {title} ({year})")

    for alt in results[1:5]:
        alt_type = alt.get("media_type", "?")
        if alt_type != detected_type:
            alt_title = alt.get("name") or alt.get("title")
            alt_year = (alt.get("first_air_date") or alt.get("release_date") or "")[:4]
            state.log.info(f"  Also found [{alt_type.upper()}]: {alt_title} ({alt_year})")
            break

    if detected_type == "tv":
        meta = tmdb_search_tv(query)
    else:
        meta = tmdb_search(query)

    return meta, detected_type


def tmdb_fetch_details(metadata):
    """Fetch full movie details from TMDb."""
    api_key = CONFIG["tmdb_api_key"]
    tmdb_id = metadata.get("tmdb_id")
    if not api_key or not tmdb_id:
        return metadata

    url = (
        f"https://api.themoviedb.org/3/movie/{tmdb_id}"
        f"?api_key={api_key}"
        f"&append_to_response=credits,release_dates"
    )

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        state.log.warning(f"  TMDb details fetch failed: {e}")
        return metadata

    metadata["genres"] = [g["name"] for g in data.get("genres", [])]

    companies = data.get("production_companies", [])
    if companies:
        metadata["studio"] = companies[0].get("name")

    metadata["runtime"] = data.get("runtime")
    metadata["tagline"] = data.get("tagline")
    metadata["backdrop_path"] = data.get("backdrop_path")

    credits = data.get("credits", {})
    directors = [
        p["name"] for p in credits.get("crew", [])
        if p.get("job") == "Director"
    ]
    metadata["directors"] = directors

    cast_list = credits.get("cast", [])[:15]
    metadata["cast"] = [
        {"name": a["name"], "role": a.get("character", "")}
        for a in cast_list
    ]

    release_dates = data.get("release_dates", {}).get("results", [])
    for country in release_dates:
        if country.get("iso_3166_1") == "US":
            certs = country.get("release_dates", [])
            for cert in certs:
                if cert.get("certification"):
                    metadata["certification"] = cert["certification"]
                    break
            break

    if directors:
        state.log.info(f"  Director(s): {', '.join(directors)}")
    if metadata["genres"]:
        state.log.info(f"  Genres: {', '.join(metadata['genres'])}")
    if metadata["cast"]:
        top3 = [a["name"] for a in metadata["cast"][:3]]
        state.log.info(f"  Cast: {', '.join(top3)}, ...")
    if metadata["certification"]:
        state.log.info(f"  Rated: {metadata['certification']}")

    return metadata


def tmdb_search_tv(query):
    """Search TMDb for a TV show. Returns metadata dict or None."""
    api_key = CONFIG["tmdb_api_key"]
    if not api_key:
        return None

    encoded_query = urllib.parse.quote(query)
    url = (
        f"https://api.themoviedb.org/3/search/tv"
        f"?api_key={api_key}&query={encoded_query}&include_adult=false"
    )

    state.log.info(f"  Searching TMDb TV for: {query}")

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        state.log.warning(f"  TMDb TV search failed: {e}")
        return None

    results = data.get("results", [])
    if not results:
        state.log.warning(f"  No TMDb TV results for '{query}'")
        return None

    hit = results[0]
    first_air = hit.get("first_air_date", "")
    year = first_air[:4] if first_air else None

    metadata = {
        "media_type": "tv",
        "title": hit.get("name", query),
        "year": year,
        "overview": hit.get("overview", ""),
        "tmdb_id": hit.get("id"),
        "poster_path": hit.get("poster_path"),
        "original_title": hit.get("original_name"),
        "vote_average": hit.get("vote_average"),
        "genres": [],
        "directors": [],
        "cast": [],
        "studio": None,
        "tagline": None,
        "runtime": None,
        "certification": None,
        "seasons": [],
        "backdrop_path": hit.get("backdrop_path"),
    }

    state.log.info(f"  TMDb TV match: {metadata['title']} ({metadata['year']})")
    if len(results) > 1:
        state.log.info(f"  ({len(results) - 1} other candidates)")
        for alt in results[1:4]:
            alt_year = alt.get("first_air_date", "")[:4]
            state.log.info(f"    - {alt.get('name')} ({alt_year})")

    metadata = tmdb_fetch_tv_details(metadata)
    return metadata


def tmdb_fetch_tv_details(metadata):
    """Fetch full TV show details from TMDb."""
    api_key = CONFIG["tmdb_api_key"]
    tmdb_id = metadata.get("tmdb_id")
    if not api_key or not tmdb_id:
        return metadata

    url = (
        f"https://api.themoviedb.org/3/tv/{tmdb_id}"
        f"?api_key={api_key}"
        f"&append_to_response=credits,content_ratings"
    )

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        state.log.warning(f"  TMDb TV details fetch failed: {e}")
        return metadata

    metadata["genres"] = [g["name"] for g in data.get("genres", [])]

    networks = data.get("networks", [])
    if networks:
        metadata["studio"] = networks[0].get("name")

    metadata["tagline"] = data.get("tagline")
    metadata["backdrop_path"] = data.get("backdrop_path")

    seasons = data.get("seasons", [])
    metadata["seasons"] = [
        {
            "season_number": s.get("season_number"),
            "name": s.get("name"),
            "episode_count": s.get("episode_count"),
            "air_date": s.get("air_date"),
        }
        for s in seasons
        if s.get("season_number", 0) > 0
    ]

    credits = data.get("credits", {})
    cast_list = credits.get("cast", [])[:15]
    metadata["cast"] = [
        {"name": a["name"], "role": a.get("character", "")}
        for a in cast_list
    ]

    creators = data.get("created_by", [])
    metadata["directors"] = [c["name"] for c in creators]

    content_ratings = data.get("content_ratings", {}).get("results", [])
    for cr in content_ratings:
        if cr.get("iso_3166_1") == "US":
            metadata["certification"] = cr.get("rating")
            break

    if metadata["seasons"]:
        state.log.info(f"  Seasons: {len(metadata['seasons'])}")
    if metadata["directors"]:
        state.log.info(f"  Created by: {', '.join(metadata['directors'])}")
    if metadata["genres"]:
        state.log.info(f"  Genres: {', '.join(metadata['genres'])}")

    return metadata


def tmdb_get_season_episodes(tmdb_id, season_num):
    """Fetch episode list for a specific season from TMDb."""
    api_key = CONFIG["tmdb_api_key"]
    if not api_key or not tmdb_id:
        return []

    url = (
        f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{season_num}"
        f"?api_key={api_key}"
    )

    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        state.log.warning(f"  TMDb season fetch failed: {e}")
        return []

    episodes = data.get("episodes", [])
    return [
        {
            "episode_number": ep.get("episode_number"),
            "name": ep.get("name", ""),
            "overview": ep.get("overview", ""),
            "air_date": ep.get("air_date", ""),
            "runtime": ep.get("runtime"),
            "still_path": ep.get("still_path"),
        }
        for ep in episodes
    ]


# ============================================================================
# METADATA CACHE
# ============================================================================

def load_metadata_cache():
    """Load the persistent metadata cache from disk."""
    cache_path = Path(CONFIG["metadata_cache"])
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_metadata_cache(cache):
    """Save metadata cache to disk."""
    cache_path = Path(CONFIG["metadata_cache"])
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=2))


# ============================================================================
# MAIN METADATA RESOLUTION
# ============================================================================

def get_disc_metadata(manual_title=None, manual_year=None, media_type=None,
                      season=None, start_episode=None):
    """
    Main metadata resolution flow:
      1. If --title was given, use that
      2. Otherwise, read the disc label
      3. Detect if TV or movie
      4. Check local cache
      5. Search TMDb
      6. Fall back to cleaned disc label
      7. Confirm with user if interactive
    """
    state.log.info("=" * 60)
    state.log.info("METADATA DETECTION")
    state.log.info("=" * 60)

    cache = load_metadata_cache()
    result = {
        "title": None, "year": None, "media_type": media_type or "auto",
        "season": season, "start_episode": start_episode, "metadata": None,
        "_user_set_type": media_type is not None,
    }

    if manual_title:
        if media_type == "tv":
            meta = tmdb_search_tv(manual_title)
        elif media_type == "movie":
            meta = tmdb_search(manual_title)
        else:
            meta, detected = tmdb_dual_search(manual_title)
            result["media_type"] = detected
        if meta:
            result["title"] = meta["title"]
            result["year"] = meta.get("year") or manual_year
            result["metadata"] = meta
            if meta.get("media_type"):
                result["media_type"] = meta["media_type"]
        else:
            result["title"] = manual_title
            result["year"] = manual_year
        return result

    raw_label = extract_disc_label()
    if not raw_label:
        state.log.warning("No disc label detected.")
        if sys.stdin.isatty():
            result["title"] = input("\nEnter the movie/show title: ").strip()
            result["year"] = input("Enter the year (or Enter to skip): ").strip() or None
            mtype = input("Type [m]ovie or [t]v? [m]: ").strip().lower()
            if mtype.startswith('t'):
                result["media_type"] = "tv"
                s = input("Season number: ").strip()
                result["season"] = int(s) if s.isdigit() else 1
                ep = input("Starting episode number [1]: ").strip()
                result["start_episode"] = int(ep) if ep.isdigit() else 1
        return result

    if raw_label in cache:
        cached = cache[raw_label]
        state.log.info(f"  Cache hit: {cached['title']} ({cached.get('year')})")
        result.update(cached)
        tmdb_id = cached.get("tmdb_id")
        if tmdb_id:
            mtype = cached.get("media_type", "movie")
            if mtype == "tv":
                meta = tmdb_search_tv(cached["title"])
            else:
                meta = tmdb_search(cached["title"])
            if meta:
                result["metadata"] = meta
        return result

    cleaned, detected_season, _detected_disc = clean_disc_label(raw_label)

    if detected_season is not None and media_type is None:
        result["media_type"] = "tv"
        result["season"] = season or detected_season
        state.log.info(f"  Auto-detected as TV show (season {result['season']})")

    meta = None
    if result["media_type"] == "tv":
        meta = tmdb_search_tv(cleaned) if cleaned else None
    elif media_type == "movie":
        meta = tmdb_search(cleaned) if cleaned else None
    else:
        if cleaned:
            meta, detected_type = tmdb_dual_search(cleaned)
            result["media_type"] = detected_type
            if detected_type == "tv" and not result.get("season"):
                result["season"] = season or 1

    if meta:
        result["title"] = meta["title"]
        result["year"] = meta.get("year")
        result["metadata"] = meta
    elif sys.stdin.isatty():
        state.log.warning(f"  No TMDb match for disc label '{raw_label}'.")
        print(f"\n  The disc label '{raw_label}' didn't match anything on TMDb.")
        print("  Please enter the title manually so we can look it up.\n")

        manual_title = input("  Title: ").strip()
        if not manual_title:
            result["title"] = cleaned or raw_label
        else:
            mtype = input("  Is this a [m]ovie or [t]v show? [m]: ").strip().lower()
            if mtype.startswith('t'):
                result["media_type"] = "tv"
                s = input("  Season number [1]: ").strip()
                result["season"] = int(s) if s.isdigit() else 1
            result["_user_set_type"] = True

            if result["media_type"] == "tv":
                meta = tmdb_search_tv(manual_title)
            else:
                meta, detected_type = tmdb_dual_search(manual_title)
                if detected_type == "tv" and result["media_type"] != "tv":
                    print("  TMDb thinks this is a TV show. Switch to TV mode? [Y/n]: ", end="")
                    if input().strip().lower() != 'n':
                        result["media_type"] = "tv"
                        if not result.get("season"):
                            s = input("  Season number [1]: ").strip()
                            result["season"] = int(s) if s.isdigit() else 1
                        meta = tmdb_search_tv(manual_title)

            if meta:
                result["title"] = meta["title"]
                result["year"] = meta.get("year")
                result["metadata"] = meta
                if meta.get("media_type"):
                    result["media_type"] = meta["media_type"]
            else:
                result["title"] = manual_title
    else:
        result["title"] = cleaned or raw_label
        state.log.warning(f"  No TMDb match. Using label: {result['title']}")

    if result["media_type"] == "auto":
        result["media_type"] = "movie"

    if sys.stdin.isatty():
        type_str = result["media_type"].upper()
        season_str = f" S{result['season']:02d}" if result.get("season") else ""
        print(f"\n  Detected [{type_str}]: {result['title']}"
              + (f" ({result['year']})" if result.get('year') else "")
              + season_str)

        user_confirmed_type = result.get("_user_set_type", False)
        if user_confirmed_type:
            confirm = input("  Accept? [Y/n/edit]: ").strip().lower()
        else:
            confirm = input("  Accept? [Y/n/edit/tv/movie]: ").strip().lower()

        if confirm == 'n':
            result["title"] = input("  Enter correct title: ").strip()
            result["year"] = input("  Enter year (or Enter to skip): ").strip() or None
            mtype = input("  Type [m]ovie or [t]v? [m]: ").strip().lower()
            if mtype.startswith('t'):
                result["media_type"] = "tv"
            else:
                result["media_type"] = "movie"
            if result["media_type"] == "tv":
                meta = tmdb_search_tv(result["title"])
            else:
                meta = tmdb_search(result["title"])
            if meta:
                result["title"] = meta["title"]
                result["year"] = meta.get("year")
                result["metadata"] = meta

        elif confirm == 'tv' and not user_confirmed_type:
            result["media_type"] = "tv"
            meta = tmdb_search_tv(result["title"])
            if meta:
                result["title"] = meta["title"]
                result["year"] = meta.get("year")
                result["metadata"] = meta

        elif confirm == 'movie' and not user_confirmed_type:
            result["media_type"] = "movie"
            meta = tmdb_search(result["title"])
            if meta:
                result["title"] = meta["title"]
                result["year"] = meta.get("year")
                result["metadata"] = meta

        elif confirm == 'edit':
            result["title"] = input(f"  Title [{result['title']}]: ").strip() or result["title"]
            result["year"] = input(f"  Year [{result.get('year', '')}]: ").strip() or result.get("year")

        if result["media_type"] == "tv":
            if not result.get("season"):
                s = input("  Season number: ").strip()
                result["season"] = int(s) if s.isdigit() else 1
            if not result.get("start_episode"):
                ep = input("  Starting episode number [1]: ").strip()
                result["start_episode"] = int(ep) if ep.isdigit() else 1

    if result.get("metadata") and result.get("title"):
        cache[raw_label] = {
            "title": result["title"],
            "year": result.get("year"),
            "media_type": result["media_type"],
            "season": result.get("season"),
            "tmdb_id": result.get("metadata", {}).get("tmdb_id"),
        }
        save_metadata_cache(cache)

    return result


# ============================================================================
# ARTWORK & NFO
# ============================================================================

def download_artwork(metadata, dest_dir):
    """Download poster and fanart from TMDb."""
    if not metadata:
        return

    poster_path = metadata.get("poster_path")
    if poster_path:
        url = f"https://image.tmdb.org/t/p/original{poster_path}"
        dest = Path(dest_dir) / "poster.jpg"
        try:
            urllib.request.urlretrieve(url, str(dest))
            state.log.info(f"  Poster saved: {dest}")
        except (urllib.error.URLError, OSError) as e:
            state.log.warning(f"  Could not download poster: {e}")

    backdrop = metadata.get("backdrop_path")
    if backdrop:
        url = f"https://image.tmdb.org/t/p/original{backdrop}"
        dest = Path(dest_dir) / "fanart.jpg"
        try:
            urllib.request.urlretrieve(url, str(dest))
            state.log.info(f"  Fanart saved: {dest}")
        except (urllib.error.URLError, OSError) as e:
            state.log.warning(f"  Could not download fanart: {e}")


def write_nfo(metadata, dest_dir, filename):
    """Write a Kodi/Jellyfin/Plex-compatible movie .nfo file."""
    if not metadata or not metadata.get("tmdb_id"):
        return

    title = metadata.get("title", "Unknown")
    year = metadata.get("year", "")
    nfo_path = Path(dest_dir) / f"{filename}.nfo"

    def esc(text):
        if not text:
            return ""
        return (str(text)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))

    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<movie>',
        f'  <title>{esc(title)}</title>',
        f'  <originaltitle>{esc(metadata.get("original_title", title))}</originaltitle>',
        f'  <year>{esc(year)}</year>',
        f'  <plot>{esc(metadata.get("overview", ""))}</plot>',
        f'  <tagline>{esc(metadata.get("tagline", ""))}</tagline>',
        f'  <runtime>{metadata.get("runtime", "")}</runtime>',
        f'  <mpaa>{esc(metadata.get("certification", ""))}</mpaa>',
        f'  <rating>{metadata.get("vote_average", "")}</rating>',
        f'  <studio>{esc(metadata.get("studio", ""))}</studio>',
        f'  <uniqueid type="tmdb">{metadata.get("tmdb_id", "")}</uniqueid>',
    ]

    for genre in metadata.get("genres", []):
        lines.append(f'  <genre>{esc(genre)}</genre>')
    for director in metadata.get("directors", []):
        lines.append(f'  <director>{esc(director)}</director>')
    for actor in metadata.get("cast", []):
        lines.append('  <actor>')
        lines.append(f'    <name>{esc(actor["name"])}</name>')
        lines.append(f'    <role>{esc(actor.get("role", ""))}</role>')
        lines.append('  </actor>')

    if metadata.get("poster_path"):
        lines.append('  <thumb aspect="poster">poster.jpg</thumb>')
    lines.append('  <fanart>')
    lines.append('    <thumb>fanart.jpg</thumb>')
    lines.append('  </fanart>')

    lines.append('</movie>')

    nfo_path.write_text('\n'.join(lines), encoding='utf-8')
    state.log.info(f"  NFO written: {nfo_path}")


def write_tv_nfo(metadata, dest_dir, show_title, season_num):
    """Write a tvshow.nfo for Kodi/Jellyfin."""
    if not metadata or not metadata.get("tmdb_id"):
        return

    nfo_path = Path(dest_dir) / "tvshow.nfo"
    if nfo_path.exists():
        return

    def esc(text):
        if not text:
            return ""
        return (str(text)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))

    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<tvshow>',
        f'  <title>{esc(show_title)}</title>',
        f'  <originaltitle>{esc(metadata.get("original_title", show_title))}</originaltitle>',
        f'  <year>{esc(metadata.get("year", ""))}</year>',
        f'  <plot>{esc(metadata.get("overview", ""))}</plot>',
        f'  <mpaa>{esc(metadata.get("certification", ""))}</mpaa>',
        f'  <rating>{metadata.get("vote_average", "")}</rating>',
        f'  <studio>{esc(metadata.get("studio", ""))}</studio>',
        f'  <uniqueid type="tmdb">{metadata.get("tmdb_id", "")}</uniqueid>',
    ]

    for genre in metadata.get("genres", []):
        lines.append(f'  <genre>{esc(genre)}</genre>')
    for actor in metadata.get("cast", []):
        lines.append('  <actor>')
        lines.append(f'    <name>{esc(actor["name"])}</name>')
        lines.append(f'    <role>{esc(actor.get("role", ""))}</role>')
        lines.append('  </actor>')

    lines.append('</tvshow>')
    nfo_path.write_text('\n'.join(lines), encoding='utf-8')
    state.log.info(f"  Show NFO written: {nfo_path}")


def write_episode_nfo(episode_info, dest_path, show_title, season_num, episode_num):
    """Write a per-episode .nfo file."""
    nfo_path = dest_path.with_suffix('.nfo')

    def esc(text):
        if not text:
            return ""
        return (str(text)
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;"))

    ep_name = episode_info.get("name", f"Episode {episode_num}") if episode_info else f"Episode {episode_num}"
    ep_plot = episode_info.get("overview", "") if episode_info else ""
    ep_aired = episode_info.get("air_date", "") if episode_info else ""

    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<episodedetails>',
        f'  <title>{esc(ep_name)}</title>',
        f'  <showtitle>{esc(show_title)}</showtitle>',
        f'  <season>{season_num}</season>',
        f'  <episode>{episode_num}</episode>',
        f'  <plot>{esc(ep_plot)}</plot>',
        f'  <aired>{esc(ep_aired)}</aired>',
        '</episodedetails>',
    ]
    nfo_path.write_text('\n'.join(lines), encoding='utf-8')
