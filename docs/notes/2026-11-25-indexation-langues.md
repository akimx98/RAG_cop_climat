# 2026-11-25 — Indexation : version française, llms.txt, image d'aperçu

*Taille : M · Statut : livré (9326e27, déployé le 25/11/2026)*
*Fichiers concernés : `src/cop_corpus/templates/base.html`, `src/cop_corpus/web.py` (`ROBOTS`, `sitemap`, `llms`),
`src/cop_corpus/static/`, `tests/test_indexation.py`*

## Problème

Le socle d'indexation est en ligne (0f9102a : sitemap, `canonical`, description). Mais la langue suit un cookie
sur une même adresse : les robots, sans cookie, ne voient que l'anglais, et la version française du site n'existe
pas pour les moteurs. Pas de `llms.txt` ni d'image pour les aperçus de partage.

## Faits mesurés

| Fait | Valeur | Comment mesuré |
|---|---|---|
| Langue d'une requête | `?lang=` d'abord, puis préférence du compte, puis cookie, sinon anglais | `web.py:124-131`, `i18n.py` |
| `?lang=xx` dans l'adresse | rend la page dans cette langue **et** pose le cookie (un an) | `web.py:158-159` |
| `robots.txt` | `Disallow: /database?` bloque aussi `/database?lang=fr` | `web.py` `ROBOTS` |
| `canonical` et `og:url` | `PUBLIC_BASE` + chemin, sans requête | `base.html` (0f9102a) |
| Sitemap en production | 10 318 adresses (dont 10 288 documents), 825 ko, 1,4 s | `curl`, 25/11 |
| Robots | Googlebot, GPTBot, ClaudeBot reçoivent les pages en anglais (200) | `curl -A`, 25/11 |
| Images servies | `/static/<nom>` : `[a-z0-9-]+.(jpg|png|webp|svg)`, cache 7 jours | `web.py:496-502` |
| Outil d'image | `rsvg-convert` sur le Mac ; pas de Pillow dans le projet | `which`, 25/11 |

Hypothèse : Google accepte des versions linguistiques distinguées par un paramètre d'URL, reliées par `hreflang`
avec `canonical` propre à chaque version (documentation Google, non relue aujourd'hui).

## Modes de panne

| Composant | Panne possible | Après le changement |
|---|---|---|
| `canonical` de la version française | pointe vers l'anglais → Google ignore le français | `canonical` propre à chaque langue (critère 1) |
| `hreflang` | versions non réciproques → ignorées | chaque page annonce les deux versions et `x-default` (critère 1) |
| Cookie posé par `?lang=fr` | un robot qui garde les cookies verrait du français sur l'adresse anglaise | les robots principaux ne gardent pas les cookies ; risque accepté |
| `robots.txt` | version française de `/database` bloquée | `Allow: /database?lang=fr$` (critère 3) |
| Sitemap | taille doublée si les 10 288 documents sont listés deux fois | seulement pages fixes et dossiers en double ; les documents restent en anglais (texte d'origine identique) |
| `llms.txt` | contenu inventé ou périmé (chiffres, promesses) | texte repris des pages du site, sans chiffre qui bouge ; liens construits avec `PUBLIC_BASE` |
| Image d'aperçu | droits d'une photo | image graphique produite par nous (logo, nom, accroche), pas de photo |

## Critères d'acceptation

1. Chaque page publique annonce `<link rel="alternate" hreflang="en">` (adresse sans `lang`),
   `hreflang="fr"` (même adresse + `?lang=fr`) et `hreflang="x-default"` (adresse sans `lang`), sur
   `PUBLIC_BASE`. `canonical` et `og:url` valent l'adresse de la langue affichée **seulement quand elle est
   demandée dans l'adresse** (`?lang=fr` → version française ; sinon adresse sans `lang`).
2. `og:locale` suit la langue affichée et `og:locale:alternate` annonce l'autre.
3. `robots.txt` autorise `/database?lang=fr` et garde les autres exclusions.
4. Le sitemap liste, pour les pages fixes et les dossiers, la version française en plus, avec les liens
   `xhtml:link` réciproques ; les documents restent listés une fois ; XML valide ; jamais de document non public.
5. `/llms.txt` (texte, UTF-8) : ce qu'est OpenCOP, pour qui, ce que contient le corpus, comment s'en servir
   (site, connecteur MCP via `/connect`), règles de citation, indépendance, licence, contact ; liens absolus sur
   `PUBLIC_BASE` ; aucun chiffre susceptible de changer.
6. `og:image` (et `twitter:card` = `summary_large_image`) vers une image PNG 1200 × 630 servie par `/static/`,
   lisible en petit ; `og:image:alt` renseigné.
7. Tests verts ; vérifications en production : `curl` des en-têtes `hreflang` en anglais et en français, sitemap
   valide, `llms.txt`, image 200.

## Conception

- **Langue dans l'adresse** : deux options. (a) Préfixe `/fr/…` : propre, mais touche toutes les routes, les liens
  internes et les redirections. (b) Paramètre `?lang=fr`, qui existe déjà : seulement des balises et le sitemap.
  **Choix : (b)**, réversible et sans risque pour les parcours existants. Les liens internes d'une page française
  ne portent pas `?lang=fr` : la découverte passe par le sitemap et les `hreflang`.
- **Documents** (`/doc/…`) : une seule version annoncée (le texte est dans sa langue d'origine) : ni `hreflang`, ni
  `canonical` en `?lang=fr`, ni doublon dans le sitemap.
- **`llms.txt`** : route texte, gabarit dans `web.py`, liens construits avec `PUBLIC_BASE`.
- **Image** : SVG source et PNG produit par `rsvg-convert`, tous deux dans `static/` (le PNG est commité ; pas de
  dépendance ajoutée).

Hors périmètre : adresses `/fr/…` ; `llms-full.txt` ; images d'aperçu par page ; traduction des documents.

## Déploiement

`scripts/ship.sh --app` (seul le site change). Retour arrière : `scripts/ship.sh --rollback`. Vérifications :
critère 7. Demander une nouvelle exploration du sitemap dans la Search Console une fois le site déclaré.

## Revue

Tour 1 (reviewer, 25/11) : prêt, aucun bloquant ; critères 1 à 6 conformes, 7 après déploiement. Constats
mineurs acceptés et corrigés :
1. `llms.txt` disait que la page document « cite » le passage : reformulé d'après `how.html`.
2. Lien du code source vide si `COP_SOURCE_URL` est vide : ligne conditionnelle.
3. `llms.txt` renvoyait vers `/privacy`, le sitemap vers `/confidentialite` : aligné sur `/confidentialite`.
5. Documents annonçant une version `?lang=fr` identique : `hreflang` et `canonical` français retirés pour `/doc/`.
7. Tests ajoutés : document en une seule version, restreint absent du sitemap, `$` de la règle `Allow`.

Accepté sans changement : 4. niveau 1 « legally binding » (libellé déjà affiché sur le site, `i18n.LEVELS_EN`).
Reporté en suite : 6. chemin non ré-encodé dans les adresses de référence ; pages privées et d'erreur sans
`noindex`.

## Suites

- Adresses de référence : ré-encoder le chemin ; `noindex` sur les pages privées et d'erreur (constat 6) : #13.
- Pages de dossier de 10 à 16 s qui figent la boucle d'événements (constaté pendant la vérification, antérieur à ce
  changement ; le sitemap invite les robots à les explorer) : traité par une autre session (« Performance du site »).

## Promotion (à la livraison)

- [x] faits → architecture §10 « Indexation et présentation publique » (quoi mettre à jour quand on change une page,
      l'accroche, le nom, le domaine, la COP) ; CLAUDE.md y renvoie ; ligne ajoutée à la liste de PITFALLS
- [x] garde-fous : `web.NOT_INDEXED` + tests (toute route classée, pages du sitemap en 200, liens de `llms.txt`,
      accroche unique)
- [x] suites → #13
- [x] statut « livré » ici et dans l'index

## Vérifications en production (25/11)

`robots.txt` (règle `Allow` et `Sitemap`) ; sitemap valide, 10 348 adresses dont 30 en français, toutes sur
opencop.org ; les 60 pages fixes et dossiers en 200 ; `/about?lang=fr` : canonical en `?lang=fr`, hreflang en/fr/
x-default, `og:locale` fr_FR, `og:image` ; `/doc/51?lang=fr` : aucun hreflang, canonical sans `lang` ; `llms.txt`
200 et ses 9 liens en 200 ; image 200, PNG 1200 × 630.
