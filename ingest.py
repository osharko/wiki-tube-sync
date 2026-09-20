#!/usr/bin/env python3
"""Ingest: da raw_subs (info.json + SRT) a pagine wiki flat-file.

Genera:
  content/pages/<video_id>.md       -> frontmatter (metadati) + corpo markdown
  content/transcripts/<video_id>.json -> { segments:[{start,end,text}], chapters:[...] }
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# Percorsi configurabili via env (per il container cron di mortewiki)
RAW = Path(os.environ.get("RAW_DIR", "/mnt/data/wiki/raw_subs"))
OUT = Path(os.environ.get("WIKI_CONTENT", "/mnt/data/wiki/wiki-src/content"))

SENT_END = re.compile(r'[.!?…]\s*$')
CLEAN = re.compile(r'<[^>]+>')
WS = re.compile(r'\s+')


def clean_text(t: str) -> str:
    t = CLEAN.sub('', t)
    t = WS.sub(' ', t)
    # remove standalone bracketed sound tags a.k.a. [Musica], [Applausi], ...
    t = re.sub(r'\[[^\]]{0,30}\]', ' ', t)
    return t.strip()


TIMELINE = re.compile(
    r'(\d{2}):(\d{2}):(\d{2})[.,](\d{1,3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[.,](\d{1,3})'
)


def parse_srt(path: Path) -> list[dict]:
    """Parse an SRT file -> list of [{'start':sec,'end':sec,'text':str}].

    Robust against blank lines separating header from text body (as in YT SRT).
    """
    cues = []
    text = path.read_text(encoding='utf-8', errors='replace')
    # Each cue starts with an optional numeric index line then a timeline line.
    # Capture everything until the next timeline line / end of file.
    pos = 0
    while True:
        # locate the next timeline header
        m = TIMELINE.search(text, pos)
        if not m:
            break
        start = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
                 + int(m.group(4)) / 1000.0)
        end = (int(m.group(5)) * 3600 + int(m.group(6)) * 60 + int(m.group(7))
               + int(m.group(8)) / 1000.0)
        # text runs from end of this timeline line until the next timeline line
        body_start = m.end()
        nxt = TIMELINE.search(text, body_start)
        body_end = nxt.start() if nxt else len(text)
        body = text[body_start:body_end]
        # strip trailing numeric index that belongs to the next cue
        lines = []
        for ln in body.splitlines():
            s = ln.strip()
            if s.isdigit():
                continue  # index line of next cue
            lines.append(s)
        txt = clean_text(' '.join(lines))
        if txt:
            cues.append({'start': start, 'end': end, 'text': txt})
        pos = m.end()
        if not nxt:
            break
    return cues


def dedup_cues(cues: list[dict]) -> list[dict]:
    """Remove YouTube rolling-window duplication.

    Each cue may start by repeating the tail of the already-collected
    transcript. Keep only the genuinely-new words of each cue.
    """
    acc: list[str] = []
    out: list[dict] = []
    for c in cues:
        words = c['text'].split()
        k = 0
        maxk = min(len(words), len(acc))
        for cand in range(maxk, 0, -1):
            if words[:cand] == acc[-cand:]:
                k = cand
                break
        new = words[k:]
        if new:
            c = dict(c)
            c['text'] = ' '.join(new)
            out.append(c)
            acc.extend(new)
    return out


def group_paragraphs(cues: list[dict], gap=1.8, max_words=55) -> list[dict]:
    """Merge consecutive cues into paragraph blocks.

    Break on silence (> gap), sentence boundary, or max length.
    """
    paras: list[dict] = []
    cur: dict | None = None
    for c in cues:
        txt = c['text']
        if not txt:
            continue
        if cur is None:
            cur = {'start': c['start'], 'end': c['end'], 'text': txt}
            continue
        time_gap = c['start'] - cur['end']
        cur_words = len(cur['text'].split())
        sentence_ended = bool(SENT_END.search(cur['text']))
        if time_gap > gap or cur_words >= max_words or (sentence_ended and cur_words >= 8):
            paras.append(cur)
            cur = {'start': c['start'], 'end': c['end'], 'text': txt}
        else:
            cur['end'] = c['end']
            cur['text'] = (cur['text'] + ' ' + txt).strip()
    if cur:
        paras.append(cur)
    return paras


def build_chapters(paras: list[dict], max_ch=8) -> list[dict]:
    """Split paragraphs into roughly equal-duration chapters."""
    if not paras:
        return []
    total = paras[-1]['end']
    n = min(max_ch, max(1, len(paras)))
    per = total / n
    chapters, cur, idx = [], None, 0
    for i, p in enumerate(paras):
        if cur is None:
            title = (p['text'][:60] + ('…' if len(p['text']) > 60 else ''))
            cur = {'start': p['start'], 'title': title}
        if (idx < n - 1 and (p['end'] - cur['start']) >= per):
            chapters.append(cur)
            cur = None
            idx += 1
    if cur:
        chapters.append(cur)
    return chapters


def fmt_ts(sec: float) -> str:
    s = int(round(sec))
    return f"{s//60}:{s%60:02d}"


def tags_from(meta: dict) -> list[str]:
    base = []
    for w in re.findall(r'[A-Za-zÀ-ÿ]{4,}', meta.get('title', '')):
        base.append(w.lower())
    return list(dict.fromkeys(base))[:5]


def ensure_dirs() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'pages').mkdir(exist_ok=True)
    (OUT / 'transcripts').mkdir(exist_ok=True)


def _frontmatter_field(path: Path, key: str) -> str:
    """Da un file .md estrae il valore di una chiave di frontmatter (stringa)."""
    try:
        t = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''
    m = re.search(rf'^{re.escape(key)}\s*:\s*"?([^"\n\r]*)', t, re.M)
    return (m.group(1).strip() if m else '')


def _frontmatter_list(path: Path, key: str) -> list[str]:
    """Estrae una lista [a, b] di frontmatter da un file .md."""
    try:
        t = path.read_text(encoding='utf-8', errors='replace')
    except OSError:
        return []
    m = re.search(rf'^{re.escape(key)}\s*:\s*\[(.*?)\]', t, re.M)
    if not m:
        return []
    return [x.strip().strip('"') for x in m.group(1).split(',') if x.strip()]


def page_slug(meta: dict, vid: str) -> str:
    """Slug della pagina/cartella: {YYYYMMDD}-{video_id} (come nel link)."""
    d = str(meta.get('upload_date') or '').strip()
    return f"{d}-{vid}" if d else vid


def vid_from_url(url: str) -> str:
    """Extract a youtube video id from a URL or return it as-is."""
    m = re.search(r'(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})', url)
    if m:
        return m.group(1)
    return url.strip()


def video_type(meta: dict, hint: str | None = None) -> str:
    """Classifica: live / short / video."""
    ls = meta.get('live_status')
    if ls and ls not in (None, 'not_live'):
        return 'live'
    if hint == 'short':
        return 'short'
    if hint == 'live':
        return 'live'
    if (meta.get('duration') or 0) < 60:
        return 'short'
    return 'video'


def ingest_video(vid: str, source: str | None = None, video_type_hint: str | None = None) -> bool:
    """Build the wiki page + transcript for one video (from raw_subs)."""
    info_f = RAW / f"{vid}.info.json"
    srt_f = RAW / f"{vid}.it.srt"
    if not info_f.exists():
        print(f"[skip] {vid}: info.json mancante", file=sys.stderr)
        return False
    meta = json.loads(info_f.read_text(encoding='utf-8'))
    if source and not meta.get('source'):
        meta['source'] = source

    cues = parse_srt(srt_f) if srt_f.exists() else []
    cues = dedup_cues(cues)
    paras = group_paragraphs(cues)
    chapters = build_chapters(paras)
    transcript = {'video_id': vid, 'segments': paras, 'chapters': chapters}
    (OUT / 'transcripts' / f"{vid}.json").write_text(
        json.dumps(transcript, ensure_ascii=False, indent=1), encoding='utf-8')

    title = meta.get('title', vid)
    # channel fallback: info.json può omettere `channel`/`uploader`
    channel = (
        meta.get('channel')
        or meta.get('uploader')
        or (meta.get('uploader_id') or '').lstrip('@')
        or ''
    )
    # tipo video: usa l'hint (es. shorts) o preserva quello già salvato
    vt = video_type(meta, video_type_hint)
    if video_type_hint is None:
        prev = _frontmatter_field(OUT / 'pages' / f"{vid}.md", 'video_type')
        if prev in ('short', 'live'):
            vt = prev
    desc = meta.get('description', '') or ''
    desc_lines = [WS.sub(' ', ln).strip() for ln in (desc.splitlines() or [''])]
    desc_one = '\n'.join(ln for ln in desc_lines if ln != '')
    desc_ser = json.dumps(desc_one, ensure_ascii=False)

    # Conserva i campi "editoriali" già salvati (playlist, indice, id) se assenti da info.json
    slug = page_slug(meta, vid)
    page_dir = OUT / 'pages' / slug
    page_dir.mkdir(parents=True, exist_ok=True)
    page_f = page_dir / f"{vid}.index.md"
    pl = meta.get('playlist') or meta.get('playlist_title') or _frontmatter_field(page_f, 'playlist') or ''
    pl_id = meta.get('playlist_id') or _frontmatter_field(page_f, 'playlist_id') or ''
    pl_idx = meta.get('playlist_index') or _frontmatter_field(page_f, 'playlist_index') or '0'

    fm = [
        '---',
        f"title: \"{title.replace(chr(34), '')}\"",
        f"video_id: {vid}",
        f"channel: \"{channel.replace(chr(34), '')}\"",
        f"embeddable: {1 if meta.get('playable_in_embed') is not False else 0}",
        f"video_type: \"{vt}\"",
        f"channel_url: {meta.get('channel_url', '')}",
        f"upload_date: {meta.get('upload_date', '')}",
        f"duration: {meta.get('duration', 0)}",
        f"duration_string: \"{meta.get('duration_string', '')}\"",
        f"view_count: {meta.get('view_count', 0)}",
        f"like_count: {meta.get('like_count', 0)}",
        f"comment_count: {meta.get('comment_count', 0)}",
        # playlist metadata (conservata anche se non in info.json)
        f"playlist: \"{pl.replace(chr(34), '')}\"",
        f"playlist_id: \"{pl_id.replace(chr(34), '')}\"",
        f"playlist_index: {pl_idx}",
        f"source: \"{(source or meta.get('source') or '').replace(chr(34), '')}\"",
        # tags: usa i concetti già presenti (dai correlati), altrimenti da titolo
        "tags: [" + ", ".join('"%s"' % t for t in (_frontmatter_list(page_f, 'tags') or tags_from(meta))) + "]",
        "description: " + desc_ser,
        '---',
        '',
        '*Scorri sotto per la **trascrizione** timestampata: clicca un blocco per saltare al punto esatto nel player.*',
        '',
    ]
    page_f.write_text('\n'.join(fm), encoding='utf-8')
    print(f"[ok] {vid}  '{title}'  {len(paras)} block, {len(chapters)} chapters, dur={meta.get('duration_string','')}")
    return True


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else ['oaLur6CdPp8', 'BOKnDdrrd1c', 'rO35mGLe0N0', 'UWetGn2RdEc']
    ids = [vid_from_url(u) for u in argv]
    ensure_dirs()
    for vid in ids:
        ingest_video(vid)
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
