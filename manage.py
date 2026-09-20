#!/usr/bin/env python3
"""Automazione pipeline: scraping canale/playlist, sync nuovi video, build wiki.

Config:   ./config.yaml  (channels, blacklist)
Comandi:
  detect <url>        Elenca i video di un canale/playlist.
  fetch  <url>        Scarica/importa un video (o playlist con list=).
  sync   <url>        Importa SOLO i nuovi video (rispetta blacklist).
  resync <url>        Ri-scarica TUTTI i video del canale (refresh) + relink.
  run                 Importa tutti i canali in config.yaml in sequenza + relink.
  build               Rigenera le pagine dai dati raw + relink.
  link                Ricalcola solo i wikilink/tag del grafo (da tutti i .md).
  status              Mostra quanti video importati.
Esempi:
  ./manage.py run
  ./manage.py sync https://www.youtube.com/@Mortebianca/videos
  ./manage.py resync https://www.youtube.com/@Mortebianca/videos
  ./manage.py build
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from ingest import (
    RAW, OUT, ingest_video, vid_from_url, ensure_dirs,
)

ROOT = Path(__file__).resolve().parent
REGISTRY = RAW / "registry.json"
CONFIG = ROOT / "config.yaml"


# ---------- config (minimal YAML for channels/blacklist) ----------
def load_config() -> dict:
    cfg = {"channels": [], "blacklist": []}
    if not CONFIG.exists():
        return cfg
    section = None
    cur = None
    for raw in CONFIG.read_text(encoding="utf-8").splitlines():
        t = raw.strip()
        if not t or t.startswith("#"):
            continue
        if re.match(r'^(channels|blacklist)\s*:$', t):
            section = t.split(":")[0].strip()
            cur = None
            continue
        if section == "channels":
            m = re.match(r'^[- ]*\s*(name|url)\s*:\s*["\']?([^"\']*)["\']?\s*$', t)
            if m:
                k, v = m.group(1).lower(), m.group(2).strip()
                if k == "name":
                    cur = {"name": v, "url": None}
                elif k == "url":
                    if cur is None:
                        cur = {"name": None, "url": None}
                    cur["url"] = v
                    cfg["channels"].append(cur)
                    cur = None
        elif section == "blacklist":
            if t.startswith("-"):
                v = t[1:].strip().strip('"').strip("'").strip()
                if re.match(r'^[A-Za-z0-9_-]{11}$', v):
                    cfg["blacklist"].append(v)
    return cfg


def is_blacklisted(vid: str) -> bool:
    return vid in (load_config().get("blacklist") or [])


def find_folder(vid: str):
    """Ritorna la cartella della pagina per un video_id (slug `<vid>` o `*-<vid>`)."""
    pd = (OUT / "pages")
    for d in pd.iterdir():
        if d.is_dir() and (d.name == vid or d.name.endswith("-" + vid)):
            if list(d.glob("*.index.md")):
                return d
    # legacy flat <vid>.md
    if (pd / f"{vid}.md").is_file():
        return pd / f"{vid}.md"
    return None


def page_index(folder) -> Path:
    if folder is None:
        return None
    if folder.is_file():  # legacy flat file
        return folder
    got = list(folder.glob("*.index.md"))
    return got[0] if got else None


def has_page(vid: str) -> bool:
    return find_folder(vid) is not None


def _tags_title(title: str) -> list[str]:
    import re as _re
    stop = set("il lo la i gli le un uno una del della dei degli di da in con su per non e ed o ma che come cosa".split())
    seen, out = set(), []
    for w in _re.findall(r"[A-Za-zÀ-ÿ]{4,}", title):
        lw = w.lower()
        if lw in stop or lw in seen:
            continue
        seen.add(lw)
        out.append(lw)
        if len(out) >= 5:
            break
    return out


def create_stub(e: dict, channel: str) -> bool:
    """Crea una pagina segnaposto per un video non scaricabile (es. 18+)."""
    vid = e["id"]
    d = OUT / "pages" / vid
    f = d / f"{vid}.index.md"
    if f.exists():
        return False
    d.mkdir(parents=True, exist_ok=True)
    title = e.get("title", vid).replace('"', "\\\"")
    tags = ", ".join('"%s"' % t for t in _tags_title(e.get("title", "")))
    fm = [
        "---",
        f'title: "{title}"',
        f'video_id: {vid}',
        f'channel: "{channel.replace(chr(34), "")}"',
        "embeddable: 0",
        "stub: 1",
        f'video_type: "{e.get("type") or "video"}"',
        f'source: "https://youtu.be/{vid}"',
        f"tags: [{tags}]",
        "---",
        "",
        "*Video non disponibile all'ingest (accesso limitato, es. 18+). Aprilo su YouTube.*",
        "",
    ]
    f.write_text("\n".join(fm), encoding="utf-8")
    print(f"[stub] {vid}\t{e.get('title','')}", flush=True)
    return True


# ---------- yt-dlp wrappers ----------
def find_yt() -> str:
    for p in [ROOT / "venv" / "bin" / "yt-dlp", Path("/usr/local/bin/yt-dlp")]:
        if p.exists():
            return str(p)
    import shutil
    return shutil.which("yt-dlp") or "yt-dlp"


YT = find_yt()


def run_yt(args: list[str], timeout: int = 600) -> str:
    r = subprocess.run([YT, "--no-warnings", *args], stdout=subprocess.PIPE,
                       stderr=subprocess.PIPE, text=True, timeout=timeout)
    if r.returncode != 0 and r.stderr.strip():
        print(r.stderr[-600:], file=sys.stderr)
    return r.stdout


def has_raw(vid: str) -> bool:
    return (RAW / f"{vid}.info.json").is_file()


def discover(url: str) -> list[dict]:
    out = run_yt(["--flat-playlist", "--print", "%(id)s\t%(title)s", "--skip-download", url])
    out_ = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t", 1)
        if len(parts) == 2 and re.match(r'^[A-Za-z0-9_-]{11}$', parts[0]):
            out_.append({"id": parts[0], "title": parts[1]})
    return out_


def discover_channel(url: str) -> list[dict]:
    """Discover ALL uploads of a channel: videos tab + shorts tab (dedup)."""
    vids = discover(url)
    seen = {e["id"] for e in vids}
    for e in vids:
        e["type"] = "video"
    shorts_url = url.replace("/videos", "/shorts")
    if shorts_url != url:
        try:
            for e in discover(shorts_url):
                if e["id"] not in seen:
                    e["type"] = "short"
                    vids.append(e)
                    seen.add(e["id"])
        except Exception:
            pass
    return vids


def download(url: str, playlist: bool = False) -> list[str]:
    cmd = [
        "--skip-download", "--write-subs", "--write-auto-subs",
        "--write-info-json", "--write-description", "--convert-subs", "srt",
        "--sub-langs", "it,it-orig,it-it",
        "-o", str(RAW / "%(id)s.%(ext)s"),
        "--print", "after_move:%(id)s",
    ]
    if playlist:
        cmd += ["--yes-playlist"]
    cmd.append(url)
    out_ = run_yt(cmd)
    return [x for x in out_.split() if re.match(r'^[A-Za-z0-9_-]{11}$', x)]


def load_reg() -> dict:
    if REGISTRY.exists():
        try:
            return json.loads(REGISTRY.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_reg(reg: dict) -> None:
    REGISTRY.write_text(json.dumps(reg, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------- md frontmatter helpers (for graph linking) ----------
MD_KEY_ORDER = ["title", "video_id", "channel", "channel_url", "upload_date", "duration",
                "duration_string", "view_count", "like_count", "comment_count",
                "playlist", "playlist_id", "playlist_index", "source", "tags", "related",
                "video_type", "description"]


def _fmt_val(v):
    if isinstance(v, list):
        return "[" + ", ".join(json.dumps(x, ensure_ascii=False) for x in v) + "]"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    return json.dumps(str(v), ensure_ascii=False)


def parse_md(path: Path) -> tuple[dict, str]:
    txt = path.read_text(encoding="utf-8", errors="replace")
    m = re.match(r'\A---\n(.*?)\n---\n?(.*)\Z', txt, re.S)
    if not m:
        return {}, txt
    meta = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if v.startswith("[") and v.endswith("]"):
            meta[k] = [x.strip().strip('"') for x in v[1:-1].split(",") if x.strip()]
        elif v.startswith('"') and v.endswith('"'):
            try:
                meta[k] = json.loads(v)
            except json.JSONDecodeError:
                meta[k] = v.strip('"')
        else:
            meta[k] = v.strip('"').strip("'")
    return meta, m.group(2)


def write_md(path: Path, meta: dict, body: str) -> None:
    lines = ["---"]
    for k in MD_KEY_ORDER:
        if k in meta and meta[k] not in (None, "", [], 0):
            lines.append(f"{k}: {_fmt_val(meta[k])}")
    for k, v in meta.items():
        if k not in MD_KEY_ORDER:
            lines.append(f"{k}: {_fmt_val(v)}")
    lines.append("---")
    lines.append("")
    path.write_text("\n".join(lines) + body, encoding="utf-8")


# ---------- graph linking (Obsidian-ready wikilinks + tags) ----------
def link_pages() -> int:
    # Modello folder: ogni pagina = content/pages/<slug>/<id>.index.md
    pages = sorted((OUT / "pages").glob("*/*.index.md"))
    metas = {}
    for p in pages:
        slug = p.parent.name
        meta, _ = parse_md(p)
        metas[slug] = {"title": meta.get("title", slug), "tags": meta.get("tags", [])}

    tagmap: dict[str, list[str]] = {}
    for slug, m in metas.items():
        for t in m["tags"]:
            tagmap.setdefault(str(t), []).append(slug)

    updated = 0
    for slug, m in metas.items():
        rel = []
        for t in m["tags"]:
            for o in tagmap.get(str(t), []):
                if o != slug and o not in rel:
                    rel.append(o)
        rel = rel[:10]
        f = OUT / "pages" / slug / f"{slug.rsplit('-',1)[-1] if '-' in slug else slug}.index.md"
        if not f.exists():
            got = list((OUT / "pages" / slug).glob("*.index.md"))
            f = got[0] if got else f
        meta, body = parse_md(f)
        body = re.sub(r'\n## Correlati\s*\n(?:- .*\n?)*', '', body)   # idempotente
        meta["related"] = rel
        block = "\n## Correlati\n" + "".join(
            f"- [[{o}|{metas[o]['title']}]]\n" for o in rel)
        write_md(f, meta, body.rstrip() + "\n" + block)
        updated += 1
    print(f"link_pages: {updated} pagine aggiornate (grafo markdown)")
    return 0


# ---------- commands ----------
def cmd_detect(url: str) -> int:
    entries = [e for e in discover(url) if not is_blacklisted(e["id"])]
    print(f"{len(entries)} video rilevati:\n")
    for e in entries:
        print(f"  {e['id']}\t{e['title']}")
    return 0


def cmd_fetch(url: str) -> int:
    playlist = "list=" in url
    ids = download(url, playlist=playlist) or [vid_from_url(url)]
    ensure_dirs()
    for vid in ids:
        if is_blacklisted(vid):
            print(f"[skip] {vid} in blacklist")
            continue
        reg = load_reg()
        reg[vid] = {"url": f"https://youtu.be/{vid}"}
        save_reg(reg)
        ingest_video(vid, source=url)
    return link_pages() or 0


def _sync_one(url: str, force: bool, channel: str = "") -> tuple[int, int]:
    cfg = load_config()
    blacklist = set(cfg.get("blacklist") or [])
    entries = discover_channel(url)
    def already(e):
        return has_raw(e["id"]) or has_page(e["id"])
    todo = [e for e in entries if e["id"] not in blacklist and (force or not already(e))]
    print(f"{len(entries)} video rilevati (incl. shorts); {len(todo)} da processare (blacklist={len(blacklist)})")
    ensure_dirs()
    ok = fail = stub = 0
    for e in todo:
        vid = e["id"]
        if is_blacklisted(vid):
            continue
        try:
            ids = download(f"https://youtu.be/{vid}")
        except Exception as ex:  # noqa: BLE001
            ids = []
            print(f"  !! {vid} errore download: {ex}", file=sys.stderr)
        if ids:
            reg = load_reg()
            reg[vid] = {"url": f"https://youtu.be/{vid}", "title": e.get("title", "")}
            save_reg(reg)
            for v in (ids or [vid]):
                ingest_video(v, source=f"https://youtu.be/{vid}", video_type_hint=e.get("type"))
            ok += 1
        elif not has_raw(vid):
            e2 = dict(e)
            e2["type"] = e.get("type") or "video"
            if create_stub(e2, channel):
                stub += 1
            else:
                fail += 1
        else:
            ok += 1
    print(f"processati={ok} falliti={fail} stub={stub}")
    return ok, fail


def _load_config_once():
    return load_config()


def channel_for(url: str) -> str:
    m = re.search(r'@([A-Za-z0-9._-]+)', url or '')
    if not m:
        return ''
    handle = m.group(1).lower()
    for ch in load_config().get("channels", []):
        cm = re.search(r'@([A-Za-z0-9._-]+)', ch.get("url", '') or '')
        if cm and cm.group(1).lower() == handle:
            return ch.get("name", "")
    return ''


def cmd_sync(url: str, dry: bool = False) -> int:
    if dry:
        entries = [e for e in discover_channel(url)
                   if not has_raw(e["id"]) and not has_page(e["id"]) and not is_blacklisted(e["id"])]
        print(f"{len(entries)} nuovi:")
        for e in entries:
            print(f"  + {e['id']}\t{e['title']}")
        return 0
    _sync_one(url, force=False, channel=channel_for(url))
    return link_pages() or 0


def cmd_resync(url: str) -> int:
    print("RESYNC (ri-scarico tutti i video del canale):")
    _sync_one(url, force=True, channel=channel_for(url))
    return link_pages() or 0


def cmd_run() -> int:
    cfg = load_config()
    for ch in cfg["channels"]:
        print(f"\n===== CANALE: {ch.get('name')} =====")
        _sync_one(ch["url"], force=False, channel=ch.get("name", ""))
        link_pages()
    print("\n=== Run completato ===")
    return 0


def cmd_build() -> int:
    ensure_dirs()
    vids = sorted(p.stem.replace(".info", "") for p in RAW.glob("*.info.json"))
    for vid in vids:
        if is_blacklisted(vid):
            continue
        ingest_video(vid)
    return link_pages() or 0


def cmd_status() -> int:
    have = sorted(p.stem.replace(".info", "") for p in RAW.glob("*.info.json"))
    cfg = load_config()
    folders = [d for d in (OUT / "pages").iterdir() if d.is_dir() and list(d.glob("*.index.md"))]
    print(f"Pagine wiki (cartelle): {len(folders)} | canali: {len(cfg['channels'])} | blacklist: {len(cfg['blacklist'])}")
    from collections import Counter
    cc = Counter()
    for d in folders:
        meta, _ = parse_md(page_index(d))
        cc[meta.get("channel") or "?"] += 1
    for ch, n in cc.items():
        print(f"  {ch}: {n}")
    return 0


def cmd_migrate() -> int:
    """Migra al layout a cartelle con filenaming univoco <video_id>.<sezione>.md."""
    pd = OUT / "pages"
    moved = 0
    def slug_vid(slug: str) -> str:
        # slug = {YYYYMMDD}-{id} (l'id può contenere '-') oppure {id}
        if len(slug) > 8 and slug[:8].isdigit() and slug[8] == '-':
            return slug[9:]
        return slug

    # 1) base flat <id>.md -> cartella <slug>/<id>.index.md
    for f in sorted(pd.glob("*.md")):
        if "." in f.stem:
            continue
        meta, _ = parse_md(f)
        vid = f.stem if f.stem != '' else ''
        up = str(meta.get("upload_date") or "").strip()
        slug = (up + "-" + vid) if up else vid
        d = pd / slug
        d.mkdir(exist_ok=True)
        idx = d / f"{vid}.index.md"
        if not idx.exists():
            idx.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
            moved += 1
        f.unlink()
    # 2) override flat <id>.<x>.md -> <folder>/<id>.<x>.md
    for f in sorted(pd.glob("*.md")):
        if "." not in f.stem:
            continue
        base, suffix = f.stem.split(".", 1)
        folder = find_folder(base)
        if folder is None or folder.is_file():
            continue
        dest = folder / f"{base}.{suffix}.md"
        if not dest.exists():
            dest.write_text(f.read_text(encoding="utf-8"), encoding="utf-8")
            moved += 1
        f.unlink()
    # 3) Normalizza i nomi dentro le cartelle (video_id dal frontmatter)
    for d in [x for x in pd.iterdir() if x.is_dir()]:
        # ---- base ----
        base_files = list(d.glob("*.index.md"))
        idx = base_files[0] if base_files else (d / "index.md" if (d / "index.md").exists() else None)
        if idx is None:
            continue
        meta, _ = parse_md(idx)
        vid = str(meta.get("video_id") or slug_vid(d.name)).strip()
        if idx.name != f"{vid}.index.md":
            idx.rename(d / f"{vid}.index.md"); moved += 1
        # ---- override ----
        for old in list(d.glob("*.md")):
            if old.name.endswith(".index.md"):
                continue
            m, _ = parse_md(old)
            name = str(m.get("name") or m.get("section") or "").strip()
            if name:
                target = f"{vid}.{name}.md"
            else:
                # tolgo un eventuale prefisso "<id>." già presente
                stem = old.stem
                if stem.startswith(vid + "."):
                    stem = stem[len(vid) + 1:]
                target = f"{vid}.{stem}.md"
            if old.name != target:
                dest = d / target
                if not dest.exists():
                    old.rename(dest); moved += 1
    print(f"migrate: {moved} file riorganizzati (naming <id>.<sezione>.md)")
    return link_pages() or 0


# ---------- playlist ----------
def discover_playlists(channel_url: str) -> list[dict]:
    """Scrape the playlist tab of a channel -> [{id,title,url}]."""
    tab = channel_url.replace("/videos", "/playlists")
    if "playlists" not in tab:
        tab = tab.rstrip("/") + "/playlists"
    out = run_yt(["--flat-playlist", "--print", "%(id)s\t%(title)s\t%(url)s", "--skip-download", tab])
    pls = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0].startswith("PL"):
            pls.append({"id": parts[0], "title": parts[1], "url": parts[2]})
    return pls


def playlist_videos(playlist_url: str) -> list[dict]:
    out = run_yt(["--flat-playlist", "--print", "%(id)s\t%(playlist_index)s\t%(title)s", "--skip-download", playlist_url])
    vids = []
    idx = 0
    for line in out.splitlines():
        parts = line.split("\t")
        if parts and re.match(r'^[A-Za-z0-9_-]{11}$', parts[0]):
            idx += 1
            try:
                index = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else idx
            except ValueError:
                index = idx
            vids.append({"id": parts[0], "index": index, "title": parts[2] if len(parts) > 2 else ""})
    return vids


def update_playlist(vid: str, pl: dict, index: int) -> bool:
    d = find_folder(vid)
    if d is None:
        return False
    f = page_index(d)
    if f is None:
        return False
    meta, body = parse_md(f)
    if meta.get("playlist"):
        return False                      # già assegnato: non sovrascrivere
    meta["playlist"] = pl["title"]
    meta["playlist_id"] = pl["id"]
    meta["playlist_index"] = index
    write_md(f, meta, body)
    return True


def cmd_playlists() -> int:
    cfg = load_config()
    mapped = 0
    for ch in cfg["channels"]:
        print(f"=== {ch.get('name')} — playlist ===")
        pls = discover_playlists(ch["url"])
        print(f"  {len(pls)} playlist trovate")
        for pl in pls:
            vids = playlist_videos(pl["url"])
            n = 0
            for v in vids:
                if update_playlist(v["id"], pl, v["index"]):
                    n += 1
            mapped += n
            print(f"    - {pl['title']}: {len(vids)} video, {n} nuovi mappati")
    print(f"\nTotale video mappati a playlist: {mapped}")
    return 0


# ---------- refresh metadati (view, like, commenti, …) ----------
def refresh_video_meta(vid: str) -> bool:
    """Re-fetch info.json (no subs) and update view/like/counts in the page."""
    try:
        run_yt(["--skip-download", "--write-info-json", "--no-write-subs",
                "-o", str(RAW / "%(id)s.%(ext)s"), "-f", "best",
                f"https://youtu.be/{vid}"])
    except Exception:
        return False
    jf = RAW / f"{vid}.info.json"
    d = find_folder(vid)
    if d is None:
        return False
    f = page_index(d)
    if f is None or not jf.exists():
        return False
    meta = json.loads(jf.read_text(encoding="utf-8", errors="replace"))
    m, body = parse_md(f)
    for k in ["view_count", "like_count", "comment_count", "duration", "duration_string", "upload_date"]:
        if k in meta:
            m[k] = meta[k]
    write_md(f, m, body)
    return True


def cmd_refresh(url: str | None = None) -> int:
    cfg = load_config()
    blacklist = set(cfg.get("blacklist") or [])
    if url:
        entries = discover(url)
        vids = [e["id"] for e in entries if e["id"] not in blacklist]
    else:
        vids = [p.stem.replace(".info", "") for p in RAW.glob("*.info.json")
                if p.stem.replace(".info", "") not in blacklist]
    print(f"Aggiorno metadati (view/like/durata) per {len(vids)} video…")
    ok = fail = 0
    for vid in vids:
        if refresh_video_meta(vid):
            ok += 1
        else:
            fail += 1
        if (ok + fail) % 100 == 0:
            print(f"  … {ok + fail}/{len(vids)}")
    print(f"Fatto. aggiornati={ok} falliti={fail}")
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, *rest = argv
    if cmd == "detect" and rest:
        return cmd_detect(rest[0])
    if cmd == "fetch" and rest:
        return cmd_fetch(rest[0])
    if cmd == "sync" and rest:
        dry = "--dry-run" in rest
        urls = [a for a in rest if a != "--dry-run"]
        return cmd_sync(urls[0] if urls else "", dry=dry)
    if cmd == "resync" and rest:
        return cmd_resync(rest[0])
    if cmd == "run":
        return cmd_run()
    if cmd == "build":
        return cmd_build()
    if cmd == "link":
        return link_pages()
    if cmd == "playlists":
        return cmd_playlists()
    if cmd == "refresh":
        return cmd_refresh(rest[0] if rest else None)
    if cmd == "migrate":
        return cmd_migrate()
    if cmd == "status":
        return cmd_status()
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
