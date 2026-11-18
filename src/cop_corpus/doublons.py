"""Doublons de contenu : le même texte entré deux fois sous deux fichiers différents.

Exemples : la déclaration du G77 publiée sur g77.org (niveau 5) et déposée sur le portail de la CCNUCC (niveau 2) ;
un texte de session récupéré sans cote ni titre, en double de sa version renseignée. Le reçu SHA-256 n'attrape que
les fichiers identiques octet pour octet ; ici on compare le texte.

Méthode : empreinte « bottom-k » (les 64 plus petites valeurs de hachage des suites de 8 mots du texte normalisé),
rangée dans ``doc_sketch`` ; deux documents qui partagent au moins 90 % de leur empreinte sont le même texte.
Le document de référence est le plus officiel : avec une cote, avec un vrai titre, au niveau le plus fort (1 → 5),
puis le plus ancien. L'autre reçoit ``duplicate_of`` : il reste consultable (et sa source citée par le document de
référence), mais sort des recherches, des nouveautés et des alertes.

Jamais entre deux versions d'un même texte (même lignée, ou cotes différentes : une révision n'est pas un doublon),
ni entre un document interne et un document public (un document public ne disparaît jamais derrière un interne).
"""

from __future__ import annotations

import re
import sqlite3
import zlib

K = 64
SHINGLE = 8
THRESHOLD = 0.9
MIN_WORDS = 120
LEVEL_RANK = {"1": 0, "2": 1, "3": 2, "4": 3, "5": 4}
_GENERIC = re.compile(r"^(t[ée]l[ée]charger|download|document|message|lettre|untitled|sans titre|\d+)\b", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS doc_sketch (doc_id INTEGER NOT NULL, h INTEGER NOT NULL, PRIMARY KEY (doc_id, h));
CREATE INDEX IF NOT EXISTS doc_sketch_h ON doc_sketch(h);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(documents)")}
    if "duplicate_of" not in cols:
        conn.execute("ALTER TABLE documents ADD COLUMN duplicate_of INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS documents_dup ON documents(duplicate_of)")
    conn.commit()


def sketch(text: str) -> list[int]:
    words = re.findall(r"[^\W_]+", text.lower())
    if len(words) < MIN_WORDS:
        return []
    hashes = {zlib.crc32(" ".join(words[i:i + SHINGLE]).encode()) for i in range(len(words) - SHINGLE + 1)}
    return sorted(hashes)[:K]


def _rank(d: sqlite3.Row) -> tuple:
    title = (d["title"] or "").strip()
    return (d["symbol"] is None, not title or bool(_GENERIC.match(title)), LEVEL_RANK.get(str(d["level"]), 5), d["id"])


def check(conn: sqlite3.Connection, doc_id: int) -> int | None:
    """Calcule l'empreinte du document et le rattache à son original s'il en est un doublon.
    Renvoie l'identifiant du document de référence quand un doublon est trouvé."""
    ensure_schema(conn)
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    if not doc or doc["status"] != "indexed":
        return None
    text = "\n".join(r["text"] for r in conn.execute("SELECT text FROM passages WHERE doc_id=? ORDER BY seq", (doc_id,)))
    hs = sketch(text)
    conn.execute("DELETE FROM doc_sketch WHERE doc_id=?", (doc_id,))
    conn.executemany("INSERT OR IGNORE INTO doc_sketch(doc_id,h) VALUES(?,?)", [(doc_id, h) for h in hs])
    conn.commit()
    if len(hs) < K // 2:
        return None
    marks = ",".join("?" * len(hs))
    rows = conn.execute(
        f"""SELECT s.doc_id, COUNT(*) AS n FROM doc_sketch s WHERE s.h IN ({marks}) AND s.doc_id<>?
            GROUP BY s.doc_id HAVING n>=? ORDER BY n DESC LIMIT 10""",
        (*hs, doc_id, int(THRESHOLD * len(hs)))).fetchall()
    for r in rows:
        other = conn.execute("SELECT * FROM documents WHERE id=?", (r["doc_id"],)).fetchone()
        if not other or other["access"] != doc["access"] or other["status"] != "indexed":
            continue
        if doc["lineage_key"] and other["lineage_key"] == doc["lineage_key"]:
            continue  # deux versions d'un même texte
        if doc["symbol"] and other["symbol"] and doc["symbol"] != other["symbol"]:
            continue  # révision, rectificatif ou autre cote : jamais un doublon
        n_other = conn.execute("SELECT COUNT(*) FROM doc_sketch WHERE doc_id=?", (other["id"],)).fetchone()[0]
        if r["n"] < THRESHOLD * max(len(hs), n_other):
            continue
        keep, drop = sorted([doc, other], key=_rank)
        canonical = keep["duplicate_of"] or keep["id"]
        if canonical == drop["id"]:
            continue
        conn.execute("UPDATE documents SET duplicate_of=? WHERE id=?", (canonical, drop["id"]))
        conn.execute("UPDATE documents SET duplicate_of=? WHERE duplicate_of=?", (canonical, drop["id"]))
        conn.execute("UPDATE documents SET duplicate_of=NULL WHERE id=?", (canonical,))
        conn.commit()
        return canonical
    return None


def backfill(conn: sqlite3.Connection, limit: int | None = None, progress=None) -> dict[str, int]:
    """Empreintes des documents qui n'en ont pas encore, puis recherche des doublons."""
    ensure_schema(conn)
    ids = [r["id"] for r in conn.execute(
        """SELECT d.id FROM documents d WHERE d.status='indexed'
             AND NOT EXISTS (SELECT 1 FROM doc_sketch s WHERE s.doc_id=d.id) ORDER BY d.id""")]
    if limit:
        ids = ids[:limit]
    found = 0
    for i, doc_id in enumerate(ids, 1):
        if check(conn, doc_id):
            found += 1
        if progress and i % 500 == 0:
            progress(f"{i}/{len(ids)} documents, {found} doublons")
    return {"documents": len(ids), "doublons": found}


def listing(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT d.id, d.title, d.level, d.url, d.duplicate_of, o.title AS o_title, o.level AS o_level, o.url AS o_url
           FROM documents d JOIN documents o ON o.id=d.duplicate_of ORDER BY d.id DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]
