# Contributing to ripper

Hey, thanks for wanting to help out! This is a weekend project that got out of hand (in the best way), and contributions are very welcome.

## The vibe

Ripper is a small, focused Python package. It's a tool for ripping your own discs, not a framework. Keep it simple, keep it fun.

## Getting started

```bash
git clone https://github.com/gofastercloud/ripper.git
cd ripper
uv run ripper --status   # check your setup
```

That's the whole dev environment. Managed by `uv` via `pyproject.toml`. Always use `uv run ripper` — never `python -m ripper` directly, since `uv` handles Python version management and dependency resolution automatically.

## How to contribute

### Found a bug?

Open an issue. Include:
- What you ran
- What happened
- What you expected
- Disc type if relevant (DVD, Blu-ray, 4K UHD)

### Want to add something?

1. Open an issue first to chat about it — saves everyone time
2. Fork and branch (`git checkout -b my-cool-thing`)
3. Make your changes
4. Test with an actual disc if you can (we know, not everyone has a pile of Blu-rays lying around)
5. Open a PR

### Code style

- The package is split into focused modules under `ripper/`. See the module guide:

  | Module | Responsibility |
  |--------|---------------|
  | `state.py` | Shared globals (`CONFIG`, `tui`, `log`, shutdown flag) |
  | `config.py` | Config loading/saving, setup wizard, profiles |
  | `helpers.py` | Subprocess runners, filesystem utilities |
  | `media.py` | Source format detection, encoding auto-tune |
  | `tui.py` | Rich TUI, logging setup |
  | `metadata.py` | TMDb API, disc label parsing, NFO/artwork |
  | `jellyfin.py` | Jellyfin library scan integration |
  | `cleanup.py` | Rip manifests, cleanup, library repair |
  | `pipeline.py` | Main orchestration: rip, compress, organize |
  | `cli.py` | Entry point, argparse, signal handling |

- Dependencies are declared in `pyproject.toml`. `uv` resolves them automatically.
- Always use `uv run ripper` to run. `uv` is the only supported way to run ripper.
- Match the existing style. If the codebase uses `snake_case`, you use `snake_case`.
- Comments are good. Novels in comments are not.

### Good first contributions

- Improving disc label parsing (there are so many weird label formats out there)
- Better error messages when MakeMKV or HandBrake aren't installed
- Linux support (currently macOS-only for drive detection)
- Adding `--dry-run` support
- Documentation improvements

### Things to be mindful of

- **Don't break the single-command experience.** `uv run ripper` should always just work.
- **Config is backwards-compatible.** Old config files should still load fine.
- **Graceful shutdown matters.** If someone hits Ctrl+C mid-rip, things should clean up properly.
- **Not everyone has fast hardware.** VideoToolbox is the default for a reason.

## Running tests

```bash
uv run pytest              # All tests
uv run pytest tests/test_media.py -v   # Single file
```

## Linting

```bash
uv run ruff check .        # Check
uv run ruff check --fix .  # Auto-fix
```

## No CLA, no fuss

Your contributions are under the same [MIT license](LICENSE) as the rest of the project. That's it. No contributor license agreements, no paperwork.

## Be kind

This is a hobby project. Be excellent to each other. If someone's PR needs work, help them out. If someone files a confusing bug report, ask nice questions.

That's all. Happy ripping!
