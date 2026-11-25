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

## Secrets

- `TMDB_READ_ACCESS` (Actions secret): TMDB API Read Access Token. Never commit it. For local runs, put it in a `.env` file (see `.env.example`).
