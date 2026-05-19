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

Three archives are produced per release, matching the target triples the `postgresql_embedded` matcher expects:

```
postgresql-16.3.0-aarch64-apple-darwin.tar.gz
postgresql-16.3.0-x86_64-apple-darwin.tar.gz
postgresql-16.3.0-x86_64-unknown-linux-gnu.tar.gz
```

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

Tag format is `v<postgres-version>+pgvector-<pgvector-version>`. The workflow parses both parts, builds on three runners in parallel (macOS arm64, macOS x86_64, linux x86_64), and attaches all three tarballs to the resulting GitHub release.

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
