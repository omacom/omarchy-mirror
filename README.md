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

The script reads each `.db` as the manifest: filename, compressed size, SHA-256, and embedded package signature. These are whole changed packages, not binary patches. It checks the size and SHA-256 of existing packages on every run, downloads missing or damaged packages to temporary files, and renames them into place after verification. Completed downloads survive a failed run; an interrupted individual download restarts on the next run. Detached `.sig` files are reproduced from the original signatures embedded in the database. The script preserves signatures; pacman verifies their authenticity using the Arch Linux ARM keyring.

Before publishing local metadata, it checks `.files` entries against `.db`, finishes every package, and re-fetches `.db` to detect upstream changes during the transfer. An older package can be absent from upstream `.files`; the script reports this and still downloads it from `.db`. Invalid databases, conflicting metadata, missing packages, and checksum failures stop the run without publishing new databases or advancing `lastsync`. Rerun after a rolling update to reuse completed downloads. The `.db`, `.files`, `.tar.gz` aliases, and any upstream database signatures are preserved as regular files.

Pruning is off by default. `--prune` removes only obsolete package and signature files in the selected repositories, after all selected repositories succeed. A lock beside the stage prevents overlapping script runs. Disk space is checked before package downloads, allowing 1 GiB of headroom. `--dry-run` only fetches package databases, hashes existing packages, and reports the required downloads; it writes nothing and does not validate `.files` or package availability.

Treat this directory as an offline staging tree. Metadata files are replaced individually, so do not serve or upload the tree during a sync. A later R2 integration should run only after a successful exit and publish packages before databases, using a separate ARM destination.

### Initial upstream inventory (September 5, 2026)

| Architecture | core | extra | alarm + aur | Package total |
| --- | ---: | ---: | ---: | ---: |
| aarch64 | 1.44 GiB | 49.01 GiB | 0.14 GiB | 50.59 GiB |
| armv7h | 1.50 GiB | 42.13 GiB | 0.19 GiB | 43.81 GiB |

Allow additional space for database files, updates, and retained old packages. The aarch64 estimate excludes one unreadable record: `findnewest-0.3-4/desc` in `extra.db` contains only NUL bytes on both Florida and New Jersey. Its record in `extra.files` is readable, but the script deliberately fails on the broken package database rather than silently omitting it or rewriting upstream metadata. A full default sync needs a corrected upstream database. The local development machine also needs a larger disk to retain the complete aarch64 mirror.

Local validation downloaded and verified all 398 aarch64 packages in `core`, `alarm`, and `aur` (1.58 GiB). Repeating the sync verified all 398 again and downloaded zero packages. The resulting tree is in the ignored `stage-arm/` directory; R2 was not used.

Run the integration tests without network access to public mirrors:

```sh
python3 -m unittest discover -s tests -v
```
