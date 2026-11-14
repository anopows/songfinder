# Hosting Songfinder

The public site is https://songfinder.andi.sh. These instructions describe an independent
installation. They contain no configuration or credentials for that service.

## Local development

Install the dependencies from the README. On macOS, the development worker can decode
with the local FFmpeg installation. Run the API and worker in separate terminals with
the same environment:

```sh
export SONGFINDER_DEVELOPMENT=1
export SONGFINDER_ORIGIN=http://127.0.0.1:8792
export SONGFINDER_DATA="$PWD/web-data-public"
.venv/bin/uvicorn songfinder.api:app --host 127.0.0.1 --port 8792
```

```sh
export SONGFINDER_DEVELOPMENT=1
export SONGFINDER_ORIGIN=http://127.0.0.1:8792
export SONGFINDER_DATA="$PWD/web-data-public"
.venv/bin/python -m songfinder.worker
```

Development mode bypasses Turnstile only for the explicit loopback origin. Keep this
mode on your own computer. Linux decoding uses the Bubblewrap path configured by
`SONGFINDER_BWRAP` (default `/opt/songfinder/bin/bwrap`). It needs an appropriate AppArmor
profile on hosts that restrict unprivileged user namespaces.

## Linux production setup

The templates target Ubuntu 24.04, Python 3.12, systemd, AppArmor and Caddy. The
application and isolation settings have been exercised on that platform. The generic
installer has not yet been exercised from scratch on a second, empty host.

1. Install Python 3.12, FFmpeg, Bubblewrap, AppArmor, rsync, uv and Caddy. Ensure `uv`
   is available to root. Clone the repository somewhere outside `/opt/songfinder`.
2. Configure DNS for your hostname and a Turnstile widget restricted to that hostname.
3. Copy `.env.example` to `/etc/songfinder.env`, owned by root with mode `0600`.
   Set `SONGFINDER_ORIGIN` to your exact HTTPS origin, and provide both Turnstile keys.
4. Generate a session secret and an Argon2 admin password hash locally, put them in
   the protected environment file, and store the chosen admin password privately.
5. Run `sudo bash deploy/install.sh`. It preserves `/etc/songfinder.env` across updates.
6. Replace the hostname in `deploy/Caddyfile.example`, merge it into your Caddyfile,
   validate the configuration, and reload Caddy. The API remains on loopback port 18794.
7. Check `/healthz`, upload a public sample through the browser, and verify admin login
   and denial of anonymous requests to the private audio endpoint.

Generate a session signing secret:

```sh
.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Generate an admin hash without putting the plaintext password in shell history:

```sh
.venv/bin/python -c 'from getpass import getpass; from argon2 import PasswordHasher; print(PasswordHasher().hash(getpass("Admin password: ")))'
```

Production startup fails if required credentials are missing. The Turnstile secret
validates challenges on the server; never put it in the frontend. Keep development
mode unset. `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, and `AUDD_API_TOKEN` are optional.

The Caddy template assumes direct connections from the internet and overwrites
`X-Real-IP`. Configure trusted proxy handling explicitly if adding another proxy.

## Services and storage

| Component | Purpose |
| --- | --- |
| `songfinder-api.service` | One HTTP worker, 384 MB memory limit |
| `songfinder-worker.service` | One durable queue worker, 1.5 GB memory limit, 150% CPU quota |
| `songfinder-purge.timer` | Daily retention cleanup, alongside hourly worker cleanup |
| `/opt/songfinder` | Root-owned code and virtual environment |
| `/etc/songfinder.env` | Root-only configuration and credentials |
| `/var/lib/songfinder` | Private SQLite jobs, audio, uploads and reference features |

Upload decoding runs without network access in Bubblewrap, with a read-only input
and a per-job output directory. The recognition worker can reach public providers,
but its systemd unit blocks private-network destinations except local DNS.

New installations begin with an empty reference cache. A deployed site with an existing
cache will have different recognition coverage and latency. No reference catalogue or
private test audio is distributed with this repository.

## Data and limits

- Up to 500 MiB per upload, in ordered 8 MiB chunks; the UI labels this 500 MB.
- The first ten minutes of sound are extracted; decoding has a 90-second deadline.
- Six-minute deadline per processing job, including decode. Queue time is additional.
- New uploads/retries pause when ten searches are queued or running. Already admitted uploads always complete into the queue, so the queue can temporarily exceed ten.
- Five new uploads per IP per hour, twenty per day. Retained-audio retries have a separate twenty-per-hour, sixty-per-day limit. All searches share the global limit of one hundred per day.
- Three concurrent uploads per IP, twenty globally. Empty reservations expire after three minutes; idle transfers expire after five, checked on admission as well as during cleanup.
- Each part can take as long as needed while making progress; ninety seconds without incoming bytes triggers a retry. The browser resumes at the server's committed offset after a dropped connection. Keep the page open during upload; if retries stop, press **Resume upload**.
- The worker shares persistent recognition budgets across jobs: Shazam 400 calls/day
  at a 15-second gap; AudD 5 calls/day at a 2-second gap. Set the corresponding
  `SONGFINDER_*_DAILY` values to lower them if needed. Individual searches are also bounded.
- Uploaded video is removed after extraction. Extracted audio expires after 14 days.
- Results expire after 90 days. Unlisted result links expose file name, supplied title,
  result and feedback; private audio and diagnostic traces require admin authentication.
- Disposable references are pruned toward 4 GiB by last use. Seeded and audio-confirmed references are protected and may keep the cache above that soft target.
- Admission subtracts outstanding upload reservations and the proposed file size from current free disk and requires at least 6 GiB remaining. In-flight uncommitted bytes are conservatively counted twice.

## Updates and recovery

Run tests first. Wait until `/healthz` shows `queue: 0`, update the checkout, and run
`sudo bash deploy/install.sh` again. Active searches are interrupted by a restart;
completed results remain in SQLite. Workers requeue interrupted jobs with retained media (up to two automatic recoveries).

```sh
curl -fsS https://songfinder.example.com/healthz
sudo systemctl status songfinder-api songfinder-worker songfinder-purge.timer --no-pager
sudo journalctl -u songfinder-api -u songfinder-worker -n 60 --no-pager
sudo systemctl start songfinder-purge.service
```

`/healthz` checks a recent worker heartbeat. Back up SQLite with its backup API, not
by copying the live main database without its WAL. Protect backups as private data
and apply the same audio-retention policy. To roll back, redeploy an earlier source
revision while keeping the protected environment and data directory.

## Follow-up releases

See [release validation](release-2026-11-13.md) for database backup/restore checks,
optional fingerprint-index migration, retained-audio retry ownership and new admin metrics.
Restart the API and worker together after upgrading. The new database columns are
added automatically; existing results remain readable. Runtime secrets do not change.

The permanent `/example` result contains only metadata from a public sample. It is a frozen application fixture, so normal visitor-result retention does not remove it. It has no retained audio or feedback/retry controls.

## Public-launch foundations (0.3)

Legal pages are served from SONGFINDER_LEGAL_DIR (default /etc/songfinder/legal). All three complete HTML files (terms.html, privacy.html, legal.html) must exist without template tokens; legal.html must include id="report". Templates live in deploy/legal-templates and require final operator/legal input. Readiness is cached for 60 seconds. Missing final content returns 404 and hides legal links/consent copy while preserving the existing service. SONGFINDER_ABUSE_EMAIL supplies the result-page Request removal mailto link. Read the mailbox daily and handle removal requests within 72 hours.

Recognizer kill switches: set SONGFINDER_SHAZAM_DAILY=0 and/or SONGFINDER_AUDD_DAILY=0, then restart only the Songfinder worker after reviewing current jobs. Zero caps refuse provider calls without waiting for the rate gap. This service remains free while the unofficial Shazam client is used; provider terms/access have not been legally approved by this code change.

Install age/rclone, deploy/backup.sh, deploy/healthcheck.sh and the four backup/health service/timer files. The backup runs at 03:50; health runs every five minutes. /etc/songfinder-backup.env contains AGE_RECIPIENT and HC_BACKUP_URL; /etc/songfinder-health.env contains HC_HEALTH_URL. /etc/songfinder-backup.conf defines rclone remote backups, scoped to the site-backups bucket and songfinder/ prefix. Files are mode 0600 owned by songfinder. Set the bucket to delete backup objects after 30 days. A separate off-host age private key is required; never store it on the service host or in git. Run a manual backup and off-host decrypt/restore drill before enabling timers. Models, uploads and audio are not included in the database backup. Health probes public HTTPS plus the 10 GB disk floor.

Implementation and automated tests are local. GitHub publication was blocked by automatic approval review; no new production release, provider change, backup upload or external notification has occurred. Final legal text, SMTP/OAuth (five·six), host capacity, backup recovery and monitoring inputs remain release gates. See five·six deploy/PUBLIC_LAUNCH.md for the coordinated rollout. Use the reviewed public export.


Local verification on 14 November 2026: 75 Python tests and 8 Node tests passed in this public checkout and the canonical private source. The maintained application files match byte for byte. The new operational units are prepared; production installation, final operator legal documents, encrypted backup restore and external alert drills remain release gates. No private Git history is included in this export.
