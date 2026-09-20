# wiki-tube-sync

Script **privati** di generazione/sync per **wiki-tube**: scraping canali YouTube,
download sottotitoli/metadati, generazione pagine Markdown, playlist, refresh metadati.

> ⚠️ Repo **privato**. Le strategie di sync restano private (la wiki è open source, ma
> qui non c'è codice pubblico).

## Requisiti
- Python 3 + `yt-dlp` (venv: `./venv/bin/yt-dlp`).
- `config.yaml` (canali + blacklist), `ingest.py` (modulo), `manage.py` (CLI),
  `ingest.sh` (wrapper).

## Config
`config.yaml`:
```yaml
channels:
  - { name: "Canale", url: "https://www.youtube.com/@Canale/videos" }
blacklist: ["VIDEO_ID"]   # video da saltare
```

## Comandi
```bash
./ingest.sh detect <url>     # elenca video
./ingest.sh sync <url>       # importa i nuovi (+ stub per age-restricted)
./ingest.sh resync <url>     # ri-download completo
./ingest.sh playlists        # mappa video→playlist
./ingest.sh refresh [url]    # aggiorna view/like/durata
./ingest.sh build            # rigenera pagine da raw
./ingest.sh migrate          # (una tantum) flat → cartelle per-video
./ingest.sh run              # tutti i canali in config.yaml
```

## Modello di output
`content/pages/YYYYMMDD-<video_id>/` con file `**<video_id>.<sezione>.md**`
(`.index.md` autogenerato, `<video_id>.<sezione>.md` per override/sezioni).

## Privacy
`raw_subs/`, `content/`, `venv/` sono dati locali: **non** committarli.
