#!/usr/bin/env python3
"""Test di valutazione #3: verifica la logica keyphrase+link+QC su un corpus sintetico.
Uso: python test_correlati.py  (deve girare dalla cartella wiki-tube-sync)"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import manage

tmp = Path(tempfile.mkdtemp(prefix="wt_test_"))
(tmp / "pages").mkdir(parents=True)
(tmp / "transcripts").mkdir()


def add(vid, title, text):
    slug = f"20230101-{vid}"
    (tmp / "pages" / slug).mkdir(parents=True)
    (tmp / "pages" / slug / f"{vid}.index.md").write_text(
        f"---\ntitle: {title}\nvideo_id: {vid}\nupload_date: '20230101'\n---\n", encoding="utf-8")
    (tmp / "transcripts" / f"{vid}.json").write_text(
        json.dumps({"segments": [{"start": 0, "end": 5, "text": text}]}), encoding="utf-8")
    return slug


# corpus sintetico
a = add("AAA", "Death Note anime", "parliamo di death note kira e la religione ortodossa e protestante")
b = add("BBB", "Death Note religioni", "death note kira ortodossia protestanti e denominazioni")
c = add("CCC", "Ricetta torta", "farina zucchero burro e lievitazione torta al cioccolato")

pass_ = fail_ = 0
def chk(name, cond):
    global pass_, fail_
    print(("PASS " if cond else "FAIL ") + name)
    if cond: pass_ += 1
    else: fail_ += 1

manage.OUT = tmp
manage.link_pages()


def tags_of(slug):
    meta, _ = manage.parse_md(tmp / "pages" / slug / f"{slug.rsplit('-',1)[-1]}.index.md")
    return meta.get("tags", [])


def related_of(slug):
    meta, _ = manage.parse_md(tmp / "pages" / slug / f"{slug.rsplit('-',1)[-1]}.index.md")
    return meta.get("related", [])


chk("keyphrases include un bigramma 'death note'", "death note" in tags_of(a))
chk("tag di A ha un concetto Death-Note", any(t in "death note kira ortodossa protestante" for t in tags_of(a)))
chk("A e B correlati (death note/religione)", b in related_of(a) and a in related_of(b))
chk("C (torta) NON correlato ad A/B", c not in related_of(a) and c not in related_of(b))
qc_a = int(manage.parse_md(tmp / "pages" / a / "AAA.index.md")[0].get("qc", 0) or 0)
chk("QC di A > 0 (collegamento forte)", qc_a > 0)
chk("QC dentro 0-100", 0 <= qc_a <= 100)

print(f"\nRESULTATO: {pass_} ok, {fail_} fail")
exit(1 if fail_ else 0)
