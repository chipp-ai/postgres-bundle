# postgres-bundle

Custom PostgreSQL binary distribution for **alchemist-desktop**'s embedded-pg slot.

## Why this exists

The `postgresql_embedded` Rust crate (used by alchemist-desktop) downloads binaries from `theseus-rs/postgresql-binaries` by default. Those builds are useful but deliberately minimal — they ship without OpenSSL linking (so `pgcrypto` is absent) and don't include any third-party extensions like `pgvector`.

Alchemist needs both. Every customer app generated from the template uses UUIDs (`uuid-ossp`), cryptographic primitives (`pgcrypto`), and vector embeddings (`pgvector`). The bundled local-dev Postgres has to match the production Cloud SQL capability set so "the same code works locally and in prod" stays a real promise — not "the same code with a footnote about extensions."

So we own a binary distribution. Same archive layout the upstream crate expects; richer contents.

## What's in each archive

- PostgreSQL 16.x built with `--with-openssl --with-libxml --with-libxslt --with-icu` (so `pgcrypto` and friends compile cleanly)
- Full standard `contrib/` extensions: `pgcrypto`, `uuid-ossp`, `citext`, `btree_gin`, `btree_gist`, `hstore`, `intarray`, `ltree`, `pg_trgm`, `tablefunc`, `xml2`, … (the result of `make install-world-bin`)
- [`pgvector`](https://github.com/pgvector/pgvector) at a pinned version, built against the same Postgres

Two archives are produced per release, matching the target triples the `postgresql_embedded` matcher expects:

```
postgresql-16.3.0-aarch64-apple-darwin.tar.gz
postgresql-16.3.0-x86_64-unknown-linux-gnu.tar.gz
```

> **No `x86_64-apple-darwin` (Intel mac).** alchemist-desktop ships
> Apple-Silicon-only (see `alchemist-ai/.github/workflows/desktop-release.yml`,
> which builds `darwin-aarch64` exclusively on `macos-14`), so nothing ever
> requests an Intel-mac PG asset. The `macos-13` runner also queue-starved
> that leg for the full job ceiling on multiple runs, blocking the release
> for a zero-consumer artifact. Re-add the matrix leg if an Intel/universal
> desktop build is ever reintroduced.

## macOS archives are relocatable

A plain `make install` on macOS leaves absolute load commands in every
binary: libpq & co. point at the CI runner's staging path and OpenSSL/ICU
point at `/opt/homebrew`, so `initdb` crashes on any other Mac. The
workflow fixes this with `scripts/macos-relocate.py fix`, which copies
every non-system dylib (libssl, libcrypto, libicu*) into `lib/`, rewrites
all load commands and install names to `@loader_path/...`, and ad-hoc
re-signs every Mach-O. `scripts/macos-relocate.py check` then fails the
build on any reference outside `/usr/lib`, `/System` or the tree itself,
and `scripts/smoke-test.sh` extracts the finished archive into a temp dir
and runs `initdb`, `pg_ctl start` and `CREATE EXTENSION vector, pgcrypto,
"uuid-ossp"` with every `DYLD_*` variable unset.

Every Mach-O in the macOS archive targets macOS 12.0 (the desktop app's
`minimumSystemVersion`). OpenSSL and ICU are built from source with that
deployment target instead of taken from Homebrew, whose bottles target the
runner's own macOS; the check fails the build on any newer `minos`.

Each archive carries a `BUNDLE_RELEASE` file at its root holding the
release tag, so the desktop can tell a cached install of the current
release from an older one at the same Postgres version.

## One-time setup

1. Create the GitHub repo `chipp-ai/postgres-bundle` (private or public, doesn't matter — the crate just walks the releases API).
2. Push this directory's contents to it:
   ```
   cd ~/code/postgres-bundle
   git init && git add -A && git commit -m "initial workflow"
   git remote add origin git@github.com:chipp-ai/postgres-bundle.git
   git push -u origin main
   ```

## Cutting a release

Tag and push — the workflow does the rest:

```
git tag v16.3.0+pgvector-0.7.4
git push origin v16.3.0+pgvector-0.7.4
```

Tag format is `v<postgres-version>+pgvector-<pgvector-version>`, with an optional `-rN` suffix for a rebuild of the same versions (e.g. `v16.3.0+pgvector-0.7.4-r3`). The workflow parses the parts, builds on two runners in parallel (macOS arm64, linux x86_64), and attaches both tarballs to the resulting GitHub release. The archive names never carry the revision; `postgresql_archive` picks the highest matching tag by semver, and `pgvector-0.7.4-r2` sorts above `pgvector-0.7.4`. (Build metadata compares as text, so after `-r9` the next revision is `-r90`, not `-r10`.)

You can also trigger a build without a tag via `workflow_dispatch` — useful for testing changes to the workflow itself.

## Pointing alchemist-desktop at this distribution

In `alchemist-ai/desktop/src-tauri/src/local_services.rs`, the embedded-pg Settings get a `releases_url` override:

```rust
settings.releases_url = "https://github.com/chipp-ai/postgres-bundle".to_string();
settings.version = VersionReq::parse("=16.3.0").unwrap();
```

The `postgresql_embedded` crate walks `<releases_url>/releases`, finds the release matching `version`, and downloads the asset whose filename matches the runner's target triple. From then on the desktop's `~/.theseus/postgresql/16.3.0/` install dir contains our binaries instead of upstream's.

## Adding a future extension

1. Add its build step to `.github/workflows/build.yml` after the pgvector step (clone source, set `PG_CONFIG`, `make && make install` into the same staging dir).
2. Document it in the "What's in each archive" section above.
3. Cut a new release. The desktop's first launch after the version bump picks up the new tarball.
