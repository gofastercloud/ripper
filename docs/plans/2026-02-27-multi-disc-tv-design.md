# Multi-Disc TV Series Fix

**Date:** 2026-02-27
**Status:** Approved

## Problem

Multi-disc single-season TV sets always assign disc 1 episode titles to all discs. `clean_disc_label` correctly parses `disc_num` from the volume label but the value is discarded (`_detected_disc`). `start_episode` therefore always defaults to 1.

In watch mode (non-interactive), the prompt path is skipped entirely — no opportunity to correct it.

## Solution: B + C

### A. Persist disc→episode map in the metadata cache

Add a top-level `"_disc_maps"` key to the existing cache JSON (backward-compatible):

```json
{
  "_disc_maps": {
    "Show Name::S01": { "1": 4, "2": 4 }
  },
  "SHOW_S01_D1": { "title": "...", "season": 1, ... }
}
```

Two new functions in `metadata.py`:

- `update_disc_map(title, season, disc_num, episode_count)` — called after ripping; writes/updates the disc map entry
- `get_disc_start_episode(title, season, disc_num)` — sums episodes from discs 1..N-1; returns `None` if data incomplete

**Post-rip hook in `pipeline.py`:** after `_rip_tv_disc_parallel` / `rip_tv_disc` returns, call `update_disc_map` with the ripped episode count. Covers both normal pipeline and watch mode.

### B. Surface `disc` through the metadata pipeline

- `get_disc_metadata` gains a `disc=None` parameter
- Stores detected/provided disc number in `result["disc"]` instead of discarding it
- When `disc > 1` and `start_episode` not explicitly set, calls `get_disc_start_episode` to auto-compute; falls back to interactive prompt or default 1

### C. `--disc N` CLI flag

New `--disc` integer argument. When provided:
- Overrides label-detected disc number in `get_disc_metadata`
- Rescue hatch for discs with no disc number in their label
- `--disc 2 --episode 5` skips cache lookup entirely (fully manual override)
- `--disc 2` alone: auto-computes from cache, prompts with disc context on cache miss

### D. Smarter interactive prompt

When `disc > 1` and cache lookup returns `None`, the prompt becomes:
`"Disc 2 detected, but no disc 1 record found. Starting episode number:"` instead of the silent `[1]` default.

## Files Changed

| File | Changes |
|------|---------|
| `metadata.py` | Surface `disc` in result; add `update_disc_map`, `get_disc_start_episode`; `disc` param on `get_disc_metadata`; smarter prompt |
| `pipeline.py` | Pass `disc` param; call `update_disc_map` after rip (2 callsites) |
| `cli.py` | Add `--disc` arg; pass to `run_pipeline` |

## Edge Cases

- **Disc 1 not ripped with this tool:** use `--disc 2 --episode 5` to override
- **Label has no disc number:** use `--disc N` to provide it
- **Aborted disc 1 rip (wrong count):** user uses `--episode` to correct; `update_disc_map` overwrites on retry
- **Watch mode:** `detected_disc` from label + cached disc map = fully automatic
- **Old cache files:** no `_disc_maps` key → falls through to prompt/default, no regression
