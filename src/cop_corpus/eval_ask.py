"""Jeu de questions de référence pour /ask, tirées des vraies questions posées (logs `demo_answers`), une ou deux par
type. À rejouer après chaque changement du prompt ou des outils : `cop eval-ask` (≈ 1 $, quelques minutes).

Les contrôles automatiques ne jugent pas la qualité ; ils signalent ce qui se voit à coup sûr : une recherche rapide
trop longue, un outil attendu jamais appelé, des citations non retrouvées, une réponse sans source. Le rapport
garde le texte intégral des réponses pour les relire côte à côte d'une version à l'autre."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config

# (id, type, langue, question, outils dont au moins un est attendu, mots au plus)
QUESTIONS: list[tuple[str, str, str, str, tuple[str, ...], int | None]] = [
    ("gcf", "recherche", "fr", "Dernier texte GCF", ("dossier", "dossiers", "rechercher", "document"), 150),
    ("gst-version", "recherche", "fr",
     "Y a-t-il une version plus récente du projet de décision sur le dialogue annuel du bilan mondial ?",
     ("dossier", "document", "dossiers"), 150),
    ("pa-4-2", "expliquer", "fr", "Que dit l'article 4.2 de l'Accord de Paris sur les NDC ?",
     ("rechercher", "lire_passage"), None),
    ("ace-16", "expliquer", "fr",
     "Explique l'article 16 du projet de texte ACE et pourquoi l'Arabie saoudite s'y oppose. Pour un journaliste.",
     ("lire_passage", "rechercher", "document"), None),
    ("nap-debutant", "expliquer", "fr",
     "Je débute : c'est quoi le texte sur les plans nationaux d'adaptation, et qu'est-ce qui a changé dans sa "
     "dernière version ?", ("comparer_versions",), None),
    ("gga-diff", "diff", "en",
     "GGA: what changed between the last two versions of the draft decision on the Global Goal on Adaptation "
     "indicators? Paragraphs added, deleted, brackets.", ("comparer_versions",), None),
    ("cover-diff", "diff", "fr", "Donne-moi le diff de la décision de couverture", ("comparer_versions",), None),
    ("mwp-notions", "notion", "fr",
     "Le projet de décision sur le programme de travail atténuation (MWP) mentionne-t-il encore le 1,5 °C et la "
     "sortie des énergies fossiles ? Depuis quand ?", ("historique_formulation",), None),
    ("ace-genre", "notion", "fr", "Mention du genre dans les textes ACE : comment ça a évolué, qui a bloqué quoi ?",
     ("historique_formulation", "comparer_versions", "crochets"), None),
    ("finance-etat", "etat", "fr", "Où en sommes-nous pour les négociations sur la finance à Antalya ?",
     ("dossier",), None),
    ("adaptation-etat", "etat", "en", "Where is the adaptation text? Who push what?",
     ("comparer_versions",), None),
    ("mwp-acteurs", "acteur", "en",
     "Compare the written positions of the AGN, AOSIS and the EU on the Mitigation Work Programme (MWP): common "
     "ground and dividing lines.", ("positions", "soumissions"), None),
    ("israel", "acteur", "fr", "Israël : présent à la COP31 ? Dans quel groupe, avec quelles positions ?",
     ("groupes", "positions", "rechercher"), None),
]


def _run_one(q: tuple, answer_fn) -> dict[str, Any]:
    qid, kind, lang, question, tools, max_words = q
    t0 = time.time()
    try:
        r = answer_fn(question, lang, lambda ev: None, purpose="demo-admin")
    except Exception as exc:  # une question qui plante ne bloque pas les autres
        return {"id": qid, "type": kind, "question": question, "error": f"{type(exc).__name__}: {exc}"}
    used = [t["tool"] for t in r.get("trace", [])]
    checks = r.get("checks", [])
    words = len((r.get("text") or "").split())
    flags = []
    if max_words and words > max_words:
        flags.append(f"trop long ({words} > {max_words} mots)")
    if tools and not set(tools) & set(used):
        flags.append("aucun de : " + ", ".join(tools))
    bad = [c for c in checks if c["status"] != "ok"]
    if bad:
        flags.append(f"{len(bad)} citation(s) non retrouvée(s)")
    if not r.get("sources"):
        flags.append("aucune source")
    return {"id": qid, "type": kind, "question": question, "words": words, "cost": r.get("cost", 0),
            "seconds": round(time.time() - t0), "tools": used, "quotes": len(checks),
            "quotes_ok": sum(c["status"] == "ok" for c in checks), "bad_quotes": [c["quote"] for c in bad],
            "sources": len(r.get("sources") or []), "flags": flags, "text": r.get("text") or ""}


def run(only: list[str] | None = None, workers: int = 4, answer_fn=None) -> tuple[list[dict[str, Any]], str]:
    from . import demo

    answer_fn = answer_fn or demo.answer
    todo = [q for q in QUESTIONS if not only or q[0] in only]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda q: _run_one(q, answer_fn), todo))
    return results, report(results)


def report(results: list[dict[str, Any]]) -> str:
    ok = [r for r in results if "error" not in r]
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# Évaluation /ask : {now}", "",
             f"{len(ok)}/{len(results)} réponses, {sum(r['cost'] for r in ok):.2f} $, "
             f"citations retrouvées {sum(r['quotes_ok'] for r in ok)}/{sum(r['quotes'] for r in ok)}", "",
             "| id | type | mots | citations ok | sources | $ | s | alertes |", "|---|---|---|---|---|---|---|---|"]
    for r in results:
        if "error" in r:
            lines.append(f"| {r['id']} | {r['type']} | – | – | – | – | – | ERREUR {r['error'][:80]} |")
        else:
            lines.append(f"| {r['id']} | {r['type']} | {r['words']} | {r['quotes_ok']}/{r['quotes']} | {r['sources']} "
                         f"| {r['cost']:.3f} | {r['seconds']} | {'; '.join(r['flags']) or '–'} |")
    for r in ok:
        lines += ["", f"## {r['id']} ({r['type']})", "", f"> {r['question']}", "",
                  f"Outils : {', '.join(r['tools']) or '–'}", ""]
        if r["bad_quotes"]:
            lines += ["Citations non retrouvées : " + " · ".join(f"« {q[:80]} »" for q in r["bad_quotes"]), ""]
        lines.append(r["text"])
    return "\n".join(lines) + "\n"


def save(text: str) -> Path:
    out = Path(config.DATA_DIR) / "evals" / f"ask-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M')}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out
