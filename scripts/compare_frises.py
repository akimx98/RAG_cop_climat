"""Vérification en production du critère 3 de la phase 2 (docs/notes/2026-11-27-phase2-site-sans-fond.md) : pour
chaque dossier, les frises stockées par le worker (table `dossier_frises`) sont-elles identiques à un recalcul par
l'ancienne méthode (copie conforme de `web_live._frises` d'avant la phase 2) ?

LECTURE SEULE : base ouverte en `mode=ro` (toute écriture lèverait une erreur), jamais par `db.connect` (qui crée
le schéma et migre). Le recalcul garde les documents découpés en mémoire (≈ 80 Mo) et prend ≈ 30 s d'un cœur : dans un conteneur
jetable plutôt que dans celui du site, par exemple

    sudo docker compose -f deploy/docker-compose.yml run --rm --no-deps -T worker python - < scripts/compare_frises.py

Sortie : une ligne par (dossier, variante) — identique, DIFFÉRENT, absent, périmé (textes changés depuis le dernier
tour du worker : relancer après une minute) ; code de sortie 1 si une frise diffère.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time


def old_frises(conn, texts: list[dict], user: dict | None) -> list[dict]:
    """Copie conforme de l'ancien `web_live._frises` (avant la phase 2)."""
    from cop_corpus import auth, formulations

    out, seen = [], set()
    for t in texts:
        if not t.get("doc_id"):
            continue
        row = conn.execute("SELECT * FROM documents WHERE id=?", (t["doc_id"],)).fetchone()
        if not row or not row["lineage_key"] or row["lineage_key"] in seen:
            continue
        seen.add(row["lineage_key"])
        if row["access"] != "public" and not auth.can_restricted(user):
            continue
        rows = formulations.lineage(conn, row, user)
        if len(rows) >= 2:
            out.append({"titre": rows[-1]["title"], "cote": rows[-1]["symbol"], "grille": formulations.grid(conn, rows)})
    return out[:4]


def main(argv: list[str]) -> int:
    from cop_corpus import config, frises, live

    path = argv[0] if argv else str(config.DB_PATH)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    # live.texts crée son schéma au premier appel : déjà en place en production, on le saute (base en lecture seule).
    live._ready.add(str(config.DB_PATH))
    bad = 0
    for d in live.dossiers():
        slug = d["slug"]
        sig, _restricted, texts = frises.state(conn, slug)
        stored = {r["variant"]: r for r in conn.execute(
            "SELECT variant, value, signature, duration_ms FROM dossier_frises WHERE slug=?", (slug,))}
        if not stored:
            print(f"{slug:28} absent")
            continue
        for variant, row in sorted(stored.items()):
            if row["signature"] != sig:
                print(f"{slug:28} {variant:9} périmé (recalcul en attente)")
                continue
            t0 = time.perf_counter()
            ref = old_frises(conn, texts, None if variant == "public" else frises._READER)
            same = json.loads(row["value"]) == json.loads(json.dumps(ref, ensure_ascii=False))
            bad += not same
            print(f"{slug:28} {variant:9} {'identique' if same else 'DIFFÉRENT'} · {len(json.loads(row['value']))} "
                  f"frise(s) · stockée en {row['duration_ms']} ms, recalculée en {time.perf_counter() - t0:.1f} s")
    print(f"{bad} différence(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
