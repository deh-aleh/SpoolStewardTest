# Contributing

How to run SpoolSteward from the source code, run the tests and publish a release. Installing it for use is described in
[docs/en/installation.md](docs/en/installation.md): that path uses the release package and the published images, not a clone.

## 1. Run it from the source code

```sh
git clone https://github.com/deh-aleh/SpoolSteward.git && cd SpoolSteward
scripts/docker-init.sh print.example.lan 192.168.1.50
# in .env: delete the line SPOOLSTEWARD_REGISTRY=... so the images are built from this folder
docker compose up -d --build
```

To update a clone: `git pull && docker compose up -d --build`. `scripts/update.sh` refuses to run in a git clone: it replaces files,
which would fight with git.

## 2. Tests

A Python 3.12 virtual environment with the locked versions:

```sh
python3.12 -m venv .venv && . .venv/bin/activate
python -m pip install --require-hashes -r requirements/build.txt
python -m pip install --require-hashes -r requirements/dev.txt
python -m pip install --no-deps --no-build-isolation -e .
python -m pytest -q
```

57 tests need a real CUPS and FreeRADIUS. Without them they are skipped. The test stack (`docker-compose.test.yml`, docker project
`spoolsteward-test`, separate from a running SpoolSteward) provides both:

```sh
mkdir -p docker/run                                             # once; CUPS puts its unix socket here
docker compose -f docker-compose.test.yml up -d --build --wait  # returns when CUPS and FreeRADIUS answer
python -m pytest -q
docker compose -f docker-compose.test.yml down
```

`docker/run` must be created by you, not by docker, or it belongs to root. With `SPOOLSTEWARD_TESTS_REQUIRE_DOCKER=1` the tests that
need the stack fail instead of being skipped; the CI (`.github/workflows/ci.yml`) runs the whole suite that way on every pull request
and before every release.

Tests are written in English (the default language). Other languages appear only in the tests of the translations.

## 3. Locked dependencies

The image and the CI install Python packages only from `requirements/`, with exact versions and SHA-256 hashes; the project itself is
installed with `--no-deps`. The source is the dependency list in `pyproject.toml`.

| File | Holds |
| --- | --- |
| `requirements/runtime.txt` | the `dependencies` of `pyproject.toml` |
| `requirements/dev.txt` | runtime + `pytest`, `pytest-asyncio`, at the versions of runtime.txt |
| `requirements/build.txt` | `pip` and `setuptools` (from `requirements/build.in`) |

They are resolved for Python 3.12 on every platform, so they hold for amd64 and arm64. After changing `pyproject.toml`, regenerate them
in this order with [uv](https://docs.astral.sh/uv/) installed outside the project's environment (each file also starts with its
command):

```sh
U="uv pip compile --generate-hashes --universal --python-version 3.12"
$U pyproject.toml -o requirements/runtime.txt
$U pyproject.toml --extra dev -c requirements/runtime.txt -o requirements/dev.txt
$U requirements/build.in -o requirements/build.txt
```

Without other options the versions already written are kept; `--upgrade` (or `--upgrade-package NAME`) moves to newer ones. A package
in `pyproject.toml` that the lock does not have stops the image build and the CI at `pip check`.

## 4. Translations and documentation

Texts in the code are English inside `_("...")`; `scripts/i18n.py` keeps the catalogs of the other languages
([docs/en/languages.md](docs/en/languages.md)). The documentation is in `docs/en` and, with the same sections, in `docs/it`: a change to
one goes into the other. Pictures follow [docs/images/README.md](docs/images/README.md).

### Adding or fixing a translation

Catalogs are gettext files: `spoolsteward/locales/<lang>/messages.po` (`msgid` = English text, `msgstr` = translation). They are read at
start and compiled in memory with Babel — no `.mo` files, no build step. Keep `%(name)s` placeholders unchanged and write a literal `%` as `%%`.

```sh
python scripts/i18n.py extract    # refresh the catalogs from the code
python scripts/i18n.py missing    # what still lacks a translation
python scripts/i18n.py check      # completeness and placeholders
pytest tests/test_i18n.py         # fails if a catalog is incomplete or has wrong placeholders
```

New language: add it to `LANGUAGES` in `spoolsteward/i18n.py` and to `LANGS` in `scripts/i18n.py`, then run `extract`.

### Rules for people writing code

- Every visible text is English inside `_("...")` (Python: `from spoolsteward.i18n import _`; templates: `{{ _("...") }}`).
- Menu labels are short on purpose and may differ from the page titles: they use `_p("menu", "Backup")` (same text, context `menu`). To translate one, give `scripts/i18n.py add` the key `"menu::Backup"`.
- Values are always named (`_("Key %(label)s revoked", label=x)`), never concatenated.
- Plurals use `ngettext` (Russian has three forms).
- In templates, text for JavaScript uses `_js(...)` (unformatted, `%(name)s` replaced by the script) or `_t(...)|tojson` (for `confirm()`).
- Cached data (queue states, counter labels) are English *keys* translated only when shown. Enumerations are translated with `_(value)` (list in `VALUE_LABELS`).
- `Decision.reason` (audit, IPP clients) stays English; `Decision.text()` is the localised sentence for the portal.

### How it works

`spoolsteward/i18n.py` keeps the request language in a `ContextVar` set by an ASGI middleware that sits inside the session middleware.
The `ContextVar` follows the request into the threads of synchronous handlers and into tasks it starts (portal scan), so `_()`
works everywhere in a request. Outside a request (schedulers) the default applies. `_()` returns trusted Markup in templates;
`_t()` returns plain text. `user.language` (migration 0007) is written at login and re-aligned if it changes from another device.


## 5. Releases

1. Set `version` in `pyproject.toml`: `1.2.0` for a final release, `1.2.0b1` for a beta.
2. Push the matching tag: `v1.2.0`, or `v1.2.0-beta.1` for a beta.
3. `.github/workflows/release.yml` runs the tests, builds the two images for amd64 and arm64
   (`ghcr.io/<owner>/spoolsteward` and `spoolsteward-updater`), and publishes the GitHub release with the package
   `spoolsteward-<version>.tar.gz` and `SHA256SUMS`. A beta becomes a GitHub pre-release.
4. If the release breaks something for those who update (a setting renamed, a manual step, data that cannot go back), give its release
   notes a `## Breaking changes` heading that says what to do. The portal and `scripts/update.sh` then show a stronger warning and ask for
   an explicit confirmation. A change of the first number of the version, or of the second while the first is `0`, counts as breaking
   anyway.

The *Run workflow* button of the release workflow builds everything for both architectures and publishes nothing.
