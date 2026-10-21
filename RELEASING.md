# Releasing

How to cut a release of `pkg-media`, and how to pick the right options.

Releases run from **Actions → Release → Run workflow** on `main`. You never
edit a version number or write a tag by hand: the workflow computes both, so
they cannot disagree. The single source of truth is `__version__` in
`src/pkg_media/__init__.py`; `pyproject.toml` reads it at build time via
`[tool.hatch.version]`.

---

## Quick reference

| Input | Values | Default |
| --- | --- | --- |
| `bump` | `patch` · `minor` · `major` · `graduate` · `initial` | `patch` |
| `prerelease` | a SemVer id like `rc.1`, or blank | blank |
| `dry_run` | `true` · `false` | `false` |

| You want | `bump` | `prerelease` |
| --- | --- | --- |
| Bug fix, no API or contract change | `patch` | blank |
| New proto field, method, service or reason; new client capability | `minor` | blank |
| A break `tools/check_compat.py` reports, or a Public API break | `major` | blank |
| Test a risky change first | same as intended | `rc.1` |
| Ship a tested prerelease as final | `graduate` | blank |
| The first `v`-tagged release (`__version__` already holds it) | `initial` | blank |
| Just see the next version | anything | — (set `dry_run: true`) |

---

## Choosing the bump

`pkg_media` carries two surfaces: the **wire contract** (`fritill.media.v1`,
in `proto/`) that the media server implements and every service calls, and
the **Python API** app backends import. Consumers pin an exact tag, so a
mistake reaches them only when they adopt it, but then quietly. When
genuinely unsure, **round up**.

### `major` — something existing breaks

The proto contract follows `tools/check_compat.py`: a change it reports as
breaking against `proto/baseline.binpb` (the last release) is a major
version, never anything less. That covers:

- removing a message, enum, service or method, or a field without
  `reserved`;
- changing a field's type, label or number, or renumbering an enum value;
- dropping a reservation;
- also (not machine-checked): changing what a status or reason means, or
  making the server refuse what it accepted before.

A breaking proto change needs the owner's sign-off. CI stays red until the
baseline moves: run `uv run --group codegen python tools/generate.py --baseline`
in a commit that does nothing else, then release with `major`.

On the Python side, `major` covers removing or renaming an exported name
(`pkg_media.__all__`, `methods`, `reasons`, `contract`, the exceptions,
`integrations.fastapi`), changing a signature incompatibly, moving a
dependency from an extra into the base install, or renaming or removing an
extra (`grpc`, `fastapi`): every install line names them.

### `minor` — new capability, nothing existing breaks

- a new proto field, message, method, service or enum value (check_compat
  passes);
- a new reason in a `.proto` table and in `reasons.REFUSALS`;
- a new client method, option with a backward-compatible default, or
  exported name;
- a new extra.

### `patch` — no API or contract change

- bug fixes that change neither the wire format nor what is accepted or
  refused;
- docstring, README, typing or proto-comment corrections;
- packaging and CI changes that don't alter what's importable or what an
  extra installs.

---

## `initial`

Publishes `__version__` **exactly as declared**, with no increment. Use it
once: for the first release under the `v<version>` tags (`v0.26.0`, the
first release as `pkg-media`; earlier releases were tagged
`saas_media_library-v<version>` in the old `fritill-team/saas_media_library_py_client`
repository; this repository, `Itqadem/pkg_media`, has no earlier tags). After that every release uses an
arithmetic bump. The tag-collision check rejects `initial` once its tag
exists, so a stray run is harmless.

## `graduate` and prereleases

`prerelease: rc.1` with `bump: minor` gives `0.27.0-rc.1`; `graduate` turns
it into `0.27.0` (a `patch` bump would skip to `0.27.1`). Prereleases are
marked as such on the GitHub release; consumers opt in by pinning the exact
tag. Use one for a contract change the media server and this package must be
validated against each other: release `rc.1`, have the server pin it, then
`graduate`.

The git tag is SemVer (`v0.27.0-rc.1`); the built files use PEP 440
(`0.27.0rc1`). The workflow compares parsed versions, so both are equal.

## `dry_run`

Runs only the plan: computes the next version, checks the tag is free and
prints a summary. Nothing is tested, committed, tagged or published.

---

## What a real run does

1. **Plan**: computes the version from `__version__`; refuses an existing
   tag and any branch but `main`.
2. **Test**: reuses `ci.yml` in full (Python 3.11 / 3.12 / 3.13, `pytest`
   with the stubs-current and packaging tests, `tools/check_compat.py`,
   build, `twine check`, the light base install).
3. **Build**: writes `__version__`, moves `proto/baseline.binpb` forward to
   this release, builds the wheel and sdist and checks the built version
   equals the plan.
4. **Tag**: commits the bump and the baseline and pushes the commit and the
   annotated tag `v<version>` **atomically**.
5. **Publish**: a **private** GitHub release with generated notes and the
   artifacts.
6. **Notify**: posts success or failure to Discord (`DISCORD_WEBHOOK_URL`).
   Without that secret the notify step logs a warning and passes: the release
   still succeeds, silently.

## Rules

- **Released versions are immutable.** Never delete and re-push a tag; ship
  a new version.
- **Releases come from `main` only.**
- **Never publish to PyPI.** The `Private :: Do Not Upload` classifier makes
  PyPI reject an upload.
- **The repository stays private**: release artifacts inherit its
  visibility.
- **The base install stays light**: `httpx` and `pydantic` only. gRPC
  packages belong in the `grpc` extra, FastAPI in `fastapi`
  (`tests/test_packaging.py` and `ci.yml` enforce it).

---

## After a release

Consumers install by pinning the exact tag:

```bash
pip install "pkg-media[grpc,fastapi] @ git+https://github.com/Itqadem/pkg_media.git@vX.Y.Z"
```

Drop the extras a consumer does not use: `[grpc]` for the gRPC transport and
`pkg_media.contract` (the media server), `[fastapi]` for the routers.
Installing needs read access to this repository; for Docker builds see
[Install in Docker](README.md#install-in-docker). Each consumer updates its
pin when it is ready; on a `major`, every pin moves deliberately.
