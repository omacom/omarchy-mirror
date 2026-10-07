# Omarchy Mirror

Dedicated mirror of Arch core/extra/multilib repositories for Omarchy hosted on Cloudflare R2.

## Stable vs edge

The stable mirror is the default for Omarchy. It's located at `https://stable-mirror.omarchy.org/`, and it typically runs one month behind the very latest. This allows the Omarchy team time to catch any incompatibilities with new libraries or tools, so that problems can be fixed before they're rolled out to everyone. The mirror may be updated more frequently as needed to address security issues or general releases.

The edge mirror tracks the very latest Arch repositories every hour. It's located at `https://mirror.omarchy.org/`. Omarchy users can switch to this using _Update > Channel > Edge_ in the Omarchy Menu.

## Arch Linux ARM: local HTTP staging

Arch Linux ARM [pushes to its official mirrors](https://archlinuxarm.org/about/mirrors), but their public HTTP repositories can be pulled independently. `bin/omarchy-mirror-stage-arm` stages packages locally using Python 3.9+ and its standard library. It defaults to the [Florida mirror](https://fl.us.mirror.archlinuxarm.org/) over HTTPS. It does not upload to R2, change existing mirror services, or register an official ARM mirror.

Start by checking the download size:

```sh
bin/omarchy-mirror-stage-arm --stage ./stage-arm --dry-run
```

Then stage the repositories:

```sh
# All current aarch64 package repositories: core, extra, alarm, aur
bin/omarchy-mirror-stage-arm --stage /mnt/arch-arm-mirror

# Smaller local test: complete repositories, about 1.6 GiB combined
bin/omarchy-mirror-stage-arm --stage ./stage-arm --repos core alarm aur

# Repeat the same command to verify local packages and download only changes
bin/omarchy-mirror-stage-arm --stage ./stage-arm --repos core alarm aur

# Both currently published architectures, with optional removal of old packages
bin/omarchy-mirror-stage-arm --stage /mnt/arch-arm-mirror \
  --arch aarch64 --arch armv7h --prune
```

Use `--upstream https://another-mirror.example/path` to select another mirror root, `--workers` to change the default four concurrent downloads, and `--retries` / `--timeout` to tune requests. Paths retain ARM's layout: `aarch64/core/core.db`, `aarch64/core/<package>`, etc. The corresponding pacman mirror pattern is `Server = https://your-mirror.example/$arch/$repo`. The retired, empty `community` repository and installation images under `os/` are outside this package-mirror scope.

The script reads each `.db` as the manifest: filename, compressed size, SHA-256, and embedded package signature. These are whole changed packages, not binary patches. It checks the size and SHA-256 of existing packages on every run, downloads missing or damaged packages to temporary files, and renames them into place after verification. Completed downloads survive a failed run; an interrupted individual download restarts on the next run. Detached `.sig` files are reproduced from the original signatures embedded in the database. The script preserves signatures; pacman verifies their authenticity using the Arch Linux ARM keyring. SHA-256 and signature preservation do not constitute cryptographic signature verification by this tool.

Before publishing local metadata, it checks shared `.files` entries against `.db`, finishes every manifest package, and re-fetches both databases and metadata signatures to detect upstream changes during the transfer. An older `.db` package can be absent from upstream `.files`; the script reports the set difference and still downloads it. A `.files`-only record is rejected by default because it may indicate a rolling update. Malformed databases, conflicting metadata, missing manifest packages, and checksum failures stop the run without publishing new databases or advancing freshness. Rerun after a rolling update to reuse completed downloads. Successful syncs preserve `.db`, `.files`, `.tar.gz` aliases, and upstream database signatures as regular files.

Recovery is explicitly for retaining packages while upstream fixes its metadata:

```sh
bin/omarchy-mirror-stage-arm --stage ./stage-arm --recover-null-records \
  --allow-files-only aarch64/extra/qemu-system-cris-9.1.2-1-aarch64.pkg.tar.xz
```

`--recover-null-records` recovers non-empty, all-NUL `.db` records only from the exact matching path in a valid `.files` database. No other malformed content is recoverable. `--allow-null-record ARCH/REPO/MEMBER` limits recovery to the named, investigated records, so a new all-NUL record fails the run; it is repeatable. `--allow-files-only ARCH/REPO/FILENAME` permits one exact, independently investigated orphan; it is repeatable and never suppresses shared-package conflicts. If either exception is actually needed, the run retains and verifies all manifest packages and their signatures, reports **degraded**, and exits **3**. It does not publish any database, advance any freshness marker, or prune any package. Existing metadata remains untouched and may already contain upstream defects from older script versions. Rerun without exceptions once upstream is corrected.

Pruning is off by default. `--prune` removes only obsolete package and signature files in the selected repositories, after all selected repositories succeed without recovery. A lock beside the stage prevents overlapping script runs. Disk space is checked before package downloads, allowing 1 GiB of headroom. At most twice the worker count is queued for transfer. `--dry-run` fetches and validates both databases, hashes existing packages, and reports required downloads; it writes nothing and does not validate package availability. It also exits 3 for degraded metadata and fails for insufficient space.

Each non-dry run writes a schema-versioned JSON report beside the stage (for example, `stage-arm.status.json`). It records `running`, `failed`, `degraded`, or `complete`, the selected repositories, manifest hashes, counts, and any recovery/omissions. `running` or `failed` never authorizes publication; individual metadata replacements are not transactional and a write failure can leave a mixed tree. Successful runs advance only each selected repository's `aarch64/<repo>/lastsync`. The old root `lastsync` is left untouched for compatibility and must not be used as a readiness signal. A partial run reports only its selected repositories; it does not certify the rest of the stage. `--json` prints the result to stdout, with progress and warnings on stderr. Exit codes: 0 complete or clean dry run; 1 failure; 2 usage; 3 degraded; 130 interrupted.

Treat this directory as a staging tree, not something to serve. Metadata files are replaced individually, so it must not be uploaded during a sync; `omarchy-mirror-upload-arm` (below) holds the stage lock for that reason.

### Initial upstream inventory (September 5, 2026)

| Architecture | core | extra | alarm + aur | Package total |
| --- | ---: | ---: | ---: | ---: |
| aarch64 | 1.44 GiB | 49.01 GiB | 0.14 GiB | 50.59 GiB |
| armv7h | 1.50 GiB | 42.13 GiB | 0.19 GiB | 43.81 GiB |

Allow additional space for database files, updates, and retained old packages. On September 5, 2026, `findnewest-0.3-4/desc` in Florida's `extra.db` contained only NUL bytes while its exact `extra.files` record was readable. The same `extra.files` contained one stale record for `qemu-system-cris-9.1.2-1-aarch64.pkg.tar.xz`; the package was absent from both `extra.db` and the upstream package directory (HTTP 404). These defects block a normal successful sync; the recovery command above can retain packages with an explicit degraded outcome.

The initial local pull verified 311 `core`, 12,895 `extra`, 75 `alarm`, and 12 `aur` packages (13,293 total, 50.59 GiB in the manifests, 51 GiB on disk). It detected a rolling update, reused 13,291 packages on rerun, fetched two changes, then verified all packages with zero downloads on the next run. Those results establish package integrity and reuse, not healthy metadata: the earlier script preserved the defective upstream `extra.db` and incorrectly treated recovery as a normal completion. The current strict/degraded distinction fixes that. Pruning remained off, retaining two obsolete packages. The ignored `stage-arm/` tree remains local; R2 was not used.

### Publishing the ARM mirror to R2

`bin/omarchy-mirror-sync-arm` is the hourly job: it stages `core`, `extra`, `alarm` and `aur` with `omarchy-mirror-stage-arm`, then uploads the stage with `omarchy-mirror-upload-arm`. ARM has its own buckets and hosts, so the x86_64 prune in `omarchy-mirror-upload` cannot reach it:

| Channel | Bucket | Host |
| --- | --- | --- |
| edge | `omarchy-mirror-arm` | `arm-mirror.omarchy.org` |
| rc | `omarchy-mirror-arm-rc` | `arm-rc-mirror.omarchy.org` |
| stable | `omarchy-mirror-arm-stable` | `arm-stable-mirror.omarchy.org` |

The buckets keep ARM's layout, so the mirror line is `Server = https://arm-stable-mirror.omarchy.org/$arch/$repo`.

Two options on the stager make it fit for unattended use:

- `--keyring FILE` checks every package against a gpgv keyring before it is staged, using the signature the database carries. Arch Linux ARM signs every package with one build key (`68B3537F39A313B3E574D06777193F152BDBE6A6`) and does not sign its databases, so this is the only check that ties a package to its builder. A package that does not verify fails the run and is not retried. Verified packages are recorded by content and keyring beside the stage (`STAGE.verified.json`), so a package staged before the keyring was given, or under another keyring, is checked on the next run and an unchanged one is not checked twice.
- `--publish-degraded` publishes upstream's databases unchanged when `--recover-null-records` or `--allow-files-only` was needed. The two defects described above have been in `extra` since at least September 5, 2026, and every Arch Linux ARM mirror serves them; without this option the stage could never publish `extra`. The exceptions stay explicit: `config/arm-allow-files-only` and `config/arm-allow-null-records` list the known ones, and a new one of either kind fails the run until it is added.

`omarchy-mirror-upload-arm` uploads packages and signatures first, then the databases, then prunes what the stage no longer holds. It holds the stage lock throughout and refuses a stage whose last run was not `complete`, did not check signatures, or did not cover every repository in the tree. Databases are compared by checksum. Packages from the local stage are compared by time against the time R2 received its copy, which costs nothing beyond the listing; that would miss a same-name replacement stamped older than its upload, so keep the clock synced and do not restore a stage with its old timestamps. Packages above 200 MiB go up in parts with their MD5 stored on the object, so files above R2's 5 GiB single-request limit work and a promote can compare them.

#### Without a local copy

`omarchy-mirror-sync-arm --stateless` keeps nothing between runs. It lists the bucket (26,738 objects with checksums in 23 seconds), and a package the bucket already holds with the database's size and MD5 is neither downloaded nor stored; only new packages pass through the machine, each checked against the database's SHA-256 and the pinned key before it is uploaded. Against the full edge bucket a run with nothing new takes about 35 seconds, and a new machine needs no 52 GiB seed.

The trade: the staged way re-reads every byte every hour, and this way trusts the checksum R2 recorded when each object was uploaded. MD5 only decides what to skip; it never admits a package.

`.github/workflows/arm-sync.yml` runs this hourly on a GitHub runner, so no machine of ours is involved. It is off until the repository variable `ARM_SYNC_IN_ACTIONS` is `true`, and needs an `arm-mirror` environment with the variable `R2_ENDPOINT` and the secrets `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY`. Turn the machine's timer off before turning this on.

rc and stable are never synced from upstream. `omarchy-mirror-promote-arm` copies one bucket onto another, server-side and by checksum. It refuses an empty source and copies again if the source's databases changed while it ran. A first full copy is about 27,000 objects and takes a few minutes; later ones copy only what changed. Nothing runs it on a schedule:

```sh
# When a release candidate is cut
bin/omarchy-mirror-promote-arm --from omarchy:omarchy-mirror-arm --to omarchy:omarchy-mirror-arm-rc

# On release day
bin/omarchy-mirror-promote-arm --from omarchy:omarchy-mirror-arm-rc --to omarchy:omarchy-mirror-arm-stable
```

`bin/setup-arm` installs the dependencies (a current rclone from rclone.org; the distribution's is years old), the pinned keyring and the hourly timer. R2 credentials come from `/etc/omarchy-mirror/r2.env` (mode 600) as `RCLONE_CONFIG_OMARCHY_*` variables; a token limited to the three ARM buckets also needs `RCLONE_CONFIG_OMARCHY_NO_CHECK_BUCKET=true`.

Cloudflare does not cache `.xz`, `.db`, `.files` or `.sig` by default, and every Arch Linux ARM package is a `.pkg.tar.xz`, so the three hosts need cache rules of their own: packages and signatures for a long time, databases for a minute. Both rules also need a status code TTL (400-499 for a minute, 500 and above not cached); without it a 404 for a file requested before it was uploaded stays cached long after the file exists.

Run the integration tests without network access to public mirrors:

```sh
python3 -m unittest discover -s tests -v
```

CI runs Python 3.9 and 3.14 plus an Arch container with pacman. The pacman test queries a published fixture database using temporary configuration, database, cache, and keyring paths; it installs nothing and does not use the host package database. These tests cover the stager only, against a local fixture server.

The published mirrors are checked separately by `.github/workflows/arm-mirror-check.yml`: every six hours, on an arm64 runner, pacman in an Arch Linux ARM container uses one of the three hosts and nothing else, with signatures required. It does a full upgrade, installs from `extra` and `aur`, and downloads the kernel.
