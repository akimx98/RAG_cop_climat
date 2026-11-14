# Catalogue des sources

Chaque fichier `sources/*.yaml` est une **liste** d'entrées. Trois types d'entrée.

## 1. Document unique (`type: doc`, défaut)

```yaml
- id: paris-agreement-en            # unique, kebab-case, stable
  type: doc
  url: https://unfccc.int/sites/default/files/english_paris_agreement.pdf
  title: Paris Agreement
  level: 1                          # 1 adopté | 2 officiel non adopté | 3 science/données | 4 compte rendu de référence | 5 position/communiqué
  org: UNFCCC                       # émetteur
  doc_type: traite                  # traite | decision | conclusions_sb | ordre_du_jour | projet_texte | ndc | btr | rapport_synthese | loi_ue | proposition_ue | conclusions_conseil | strategie_nationale | rapport_scientifique | donnees | compte_rendu | communique | analyse | programme | autre
  session: COP21                    # optionnel : COP21…COP31, CMA1…CMA8, SB62…SB64
  symbol: FCCC/CP/2015/10/Add.1     # optionnel : cote officielle
  parties: [EU]                     # optionnel : pays (ISO3) ou groupes (EU, G77, AOSIS, LMDC, LDC, AGN, AILAC, UG, EIG, BASIC, ARAB)
  topics: [mitigation, temperature_goal]   # optionnel, vocabulaire libre court en anglais
  lang: en
  published: 2015-12-12             # optionnel, AAAA-MM-JJ
```

## 2. Exploration (`type: crawl`)

Part de pages de départ, suit les liens dont l'URL matche `follow`, récupère les
documents dont l'URL matche `collect`. Les métadonnées (`level`, `org`,
`doc_type`, `session`…) s'appliquent à tout ce qui est collecté.

```yaml
- id: unfccc-ndc-registry
  type: crawl
  start: [https://unfccc.int/NDCREG]
  follow: ['^https://unfccc\.int/NDCREG']       # regex Python, optionnel
  collect: ['^https://unfccc\.int/sites/default/files/NDC/.*\.pdf$']
  max_depth: 2
  max_docs: 400
  level: 2
  org: UNFCCC
  doc_type: ndc
  title_from: link_text                          # link_text | page_title | filename
  watch_minutes: 0                               # >0 : re-relevé périodiquement pendant la COP
```

## 3. Flux (`type: rss`)

```yaml
- id: carbonbrief-rss
  type: rss
  url: https://www.carbonbrief.org/feed/
  level: 4
  org: Carbon Brief
  doc_type: analyse
  keywords: [COP, climate talks, UNFCCC]   # optionnel : filtre sur titre/résumé
  watch_minutes: 60
```

`watch_minutes` > 0 marque une source surveillée en continu par le veilleur.
