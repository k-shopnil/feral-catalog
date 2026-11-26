# feral-catalog

The catalog pipeline for **feral**, a personal, non-commercial Android TV app for a household's ISP-provided media library.

It will:
1. Crawl the storage server's directory listings.
2. Parse file and folder names and match them to [TMDB](https://www.themoviedb.org/).
3. Read media headers (codecs, audio and subtitle tracks).
4. Publish a static, versioned catalog (snapshot + deltas) to GitHub Pages for the TV app to sync.

It runs on a schedule in GitHub Actions. No video is stored or proxied here: the app streams directly from the storage server.

> This product uses the TMDB API but is not endorsed or certified by TMDB.

## Status

Phase 1 gate: the `reachability` workflow checks that GitHub runners can reach the storage server and TMDB.

## Storage servers

`config/sources.yaml` lists every server and the folders indexed on each: `movie.ctgfun.com` (Apache),
`data.ctgfun.com` and `ftp.ctgfun.com` (nginx; both listing formats are parsed). Each run crawls them in turn.

- The first server keeps un-prefixed state paths (`disk1/English Movies/...`), so its `state.db` rows, work keys,
  `overrides.yaml` patterns and published version ids are the same as before multi-source support. Every other
  server's paths are namespaced `<id>:` (`data:disk1/disk1/...`, `ftp:English/...`) because the servers reuse
  folder names. No migration: an older indexer simply ignores the namespaced rows.
- Copies of one title on several servers are matched once and published as versions of the same title/episode.
  A file with the same name as one on a higher server is skipped; other servers' versions get
  `· Data server` / `· FTP server` appended to their label.
- A server that is down is skipped for that run (a failed listing never marks files missing). The phone app's
  connection check only requires the first server.

## Secrets

- `TMDB_READ_ACCESS` (Actions secret): TMDB API Read Access Token. Never commit it. For local runs, put it in a `.env` file (see `.env.example`).

## Running from a phone (android/)

`android/` is a small Android app ("feral catalog") that runs this same `catalog` package on the phone through
[Chaquopy](https://chaquo.com/chaquopy/) (Python 3.12), so the PC doesn't have to stay on. The package and `config/`
are copied into the APK at build time, so the phone always runs the repo's code.

- Build: `cd android && .\gradlew.bat :app:assembleDebug` (needs Python 3.12 on the PC for Chaquopy's pip step).
- **It only works on a network that can reach `movie.ctgfun.com` (BDIX).** "Check connection" tests that first.
- Tokens are baked into the (private, never-published) APK from `android/secrets.properties`
  (git-ignored: `TMDB_READ_ACCESS=...`, `GITHUB_TOKEN=...` and the optional `FANART_API_KEY=...`). Setup can override them; overrides are stored
  encrypted on the phone. Never commit that file or upload the APK anywhere: this repo is public.
- **Quick update** (top of the app) = checks, crawl, publish anything new. The advanced section keeps the
  dry-run / force-publish / full re-crawl switches.
- The phone publishes through the GitHub API (`publish.push_pages_api`), not `git push`.

### Moving the indexer between PC and phone (automatic, through Google Drive)

`catalog/gdrive.py` hands `state.db` over through the owner's Google Drive, so either device can just run:

1. take a lock on Drive (the other device refuses to start while it's held; abandoned locks expire after 4 h),
2. download Drive's newest `state.db` if it is newer than the local one (checked by generation + sha256),
3. run the normal pipeline,
4. upload the `state.db` (gzip, about 25 MB) as the next generation and release the lock.

Drive keeps the last 10 generations in the folder `feral-catalog-state` (scope `drive.file`: the app only sees
files it created). Credentials: `GDRIVE_CLIENT_ID`, `GDRIVE_CLIENT_SECRET`, `GDRIVE_REFRESH_TOKEN` in
`feral-catalog/.env` (git-ignored) and `android/secrets.properties`. One-time sign-in:
`python -m catalog drive-auth --file client_secret_*.json`. Other commands: `drive-status`, `drive-pull`,
`drive-push`, `drive-unlock` (clears a stuck lock). `run --no-drive` skips the hand-off.

Without Drive (or as a fallback) the manual way still works: `export-state --file state.db` / the app's
**Export state** on the device that ran last, then **Import state** / `import-state --file state.db` on the other
(the old state is kept as `data/state.db.bak-<time>`).

A guard refuses to run when the published catalog is newer than the local `state.db`, so a stale device
(e.g. the PC's scheduled task after a trip) can't roll the catalog back. Each run also downloads any
published snapshot/delta files its `site/` is missing.
