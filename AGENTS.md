# AGENTS.md — OPDS-ABS repository guide

## Project purpose

OPDS-ABS is a FastAPI service that exposes an Audiobookshelf library as an
OPDS 1.2 catalog. It authenticates clients, fetches and caches metadata from
Audiobookshelf, renders Atom/XML feeds, and proxies ebook downloads and image
assets through the OPDS host.

The project is a public GitHub fork of `petr-prikryl/OPDS-ABS`:
`https://github.com/trevorswanson/OPDS-ABS`.

## Architecture

The runtime path is:

1. `run.py` starts Uvicorn and imports the FastAPI app.
2. `opds_abs/main.py` is the composition root and HTTP route layer. It wires
   the application lifespan, logging, templates, static files, exception
   handlers, authentication dependencies, cache lifecycle, and feed
   generators.
3. `opds_abs/feeds/*.py` contains feed-specific generators for libraries,
   navigation, series, collections, authors, and search.
4. `opds_abs/core/feed_generator.py` provides shared OPDS/Atom XML generation,
   metadata mapping, acquisition links, pagination, and response creation.
5. `opds_abs/api/client.py` is the Audiobookshelf API adapter. It performs
   authenticated `aiohttp` requests, maps upstream failures, and uses the
   cache helpers.
6. `opds_abs/utils/auth_utils.py` handles Basic/API-key/Bearer credentials and
   token caching. `cache_utils.py` handles in-memory and optional pickle-backed
   persistence. `error_utils.py` maps failures to application/OPDS responses.
7. `opds_abs/templates/` and `opds_abs/static/` provide the web UI and static
   fallback images.

### Authentication and proxy boundaries

- Client authentication is performed by `get_authenticated_user` and
  `require_auth`; do not duplicate credential parsing in routes.
- Audiobookshelf tokens are server-side values. Never put credentials or
  bearer tokens in new image URLs, logs, test output, or documentation.
- Book covers and author images use OPDS-relative proxy routes so OPDS clients
  authenticate to one host. Ebook downloads use the existing download proxy.
- Preserve safe upstream error mapping: authentication failures should not
  expose tokens, internal URLs, or upstream response bodies.

### Configuration

`opds_abs/config.py` is the authoritative configuration module. It reads
environment variables for Audiobookshelf internal/external URLs, auth,
cache, pagination, logging, and `OPDS_RELOAD`.

The root `config.py` is a legacy compatibility module. Do not add new settings
there; update `opds_abs/config.py` and document externally visible settings in
`README.md`.

Production/container defaults must keep `OPDS_RELOAD=false`. Set
`OPDS_RELOAD=true` only for deliberate local development hot reload.

## Repository conventions

- Python 3.11 runtime in the Docker image.
- Dependencies are in `requirements.txt`; development tools are in
  `requirements-dev.txt`.
- Runtime data belongs under `opds_abs/data/`; the persisted cache is generated
  state and must not be committed.
- Keep secrets in environment/secret stores. `.env` is ignored and must not be
  committed.
- Use conventional commits (`feat:`, `fix:`, `test:`, `docs:`, `ci:`,
  `chore:`) and keep changes narrowly scoped.
- Add regression coverage for behavior changes. Tests currently use the
  standard-library `unittest` runner; do not assume pytest is installed.
- Preserve unrelated changes and inspect `git diff` before committing.

## Documentation standards

`DOCS_STANDARDS.md` is authoritative for Python docstrings:

- Use Google-style docstrings.
- Public classes, methods, and functions need docstrings unless an explicit
  configured exception applies.
- The first line is a concise, capitalized summary ending in `.`, `?`, or `!`.
- Document arguments with types, return values, and meaningful exceptions.
- Add examples/notes only when they clarify non-obvious behavior.

Relevant configuration files:

- `.pydocstyle`: Google convention and project exclusions.
- `.pre-commit-config.yaml`: pydocstyle, whitespace/EOF/YAML/large-file
  checks, and the repository docstring checker.
- `setup.cfg`: Flake8 line length (100), exclusions, and docstring settings.
- `.pylintrc`: Pylint defaults and Python version assumptions.

## Required validation

From the repository root:

```bash
# Fast regression suite
python3 -m unittest discover -s tests -v

# Syntax/bytecode validation
python3 -m compileall -q .

# Documentation checks
./docstring-check.py --summary

# Pre-commit checks, when installed
pre-commit run --all-files

# Container validation
docker build -t opds-abs:test .
 docker run --rm -p 18000:8000 \
   -e AUDIOBOOKSHELF_INTERNAL_URL=http://127.0.0.1:9 \
   -e AUDIOBOOKSHELF_EXTERNAL_URL=https://example.invalid \
   -e OPDS_RELOAD=false \
   opds-abs:test
```

The Docker smoke test should show normal Uvicorn startup, no `StatReload`, and
an unauthenticated `/opds/proxy/cover/<id>` request should return `401` with a
Basic authentication challenge.

## CI and release flow

GitHub Actions workflows live in `.github/workflows/`:

- `docstring-check.yml` runs documentation checks for Python changes.
- `docker-image-dev.yml` builds and publishes the `dev` image as
  `ghcr.io/trevorswanson/opds-abs:dev-<short-sha>`.
- Keep this as the single dev-image workflow; do not add a duplicate workflow
  with the same trigger and output tag.
- `docker-image.yml` builds and publishes `ghcr.io/trevorswanson/opds-abs:latest`
  from `master`.

Workflows use GitHub's job-scoped `GITHUB_TOKEN` with `packages: write`; do not
introduce a personal access token unless the GitHub Actions design genuinely
requires one.

Branch policy:

- Feature/fix branches open pull requests into `dev`.
- Merging into `dev` is the development image publication point.
- `master` is promotion-only and is protected by the `Protect master` ruleset:
  updates require a pull request, force-pushes are blocked, and deletion is
  blocked. Zero approvals are required because this is a single-maintainer
  repository.
- The intended promotion is `dev -> master`. GitHub's ruleset can enforce the
  pull request requirement but cannot natively require a specific source branch;
  do not silently promote another branch.

## Change guidance

When changing feeds, check every affected feed type and the generated XML,
including links, MIME types, pagination, and authentication behavior. When
changing upstream API calls, reuse `fetch_from_api` and its error/cache
semantics rather than opening ad hoc unauthenticated sessions.

When adding a route:

1. Decide whether it requires `get_authenticated_user` or `require_auth`.
2. Validate path identifiers and avoid reflecting sensitive values.
3. Map upstream failures to safe HTTP/OPDS responses.
4. Add a focused regression test.
5. Update `README.md` for user-visible configuration or behavior.
6. Run the validation commands above and inspect the resulting diff.

Do not deploy this repository directly into the homelab from a local checkout.
The live Defiant deployment is managed separately through the homelab GitOps
repository and Dockhand after review and approval.
