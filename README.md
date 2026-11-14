# COP Tracker — COP31

Corpus vérifiable, veille en temps réel et alertes pour suivre les négociations de la COP31
(Antalya, 9–20 novembre 2026). Projet indépendant, gratuit et non commercial, sans lien avec la CCNUCC
ni avec une délégation. Code sous licence MIT ([LICENSE](LICENSE)). Architecture : [docs/architecture.md](docs/architecture.md).

```
sources (ODS/ONU, EUR-Lex, Conseil UE, GIEC, PNUE, ENB, Carbon Brief, OWID…)
   │  cop fetch / veilleur (worker)       reçus SHA-256, versions, anti-bot respecté
   ▼
data/cop.sqlite3  documents · passages (FTS5) · vecteurs · utilisateurs · sujets · alertes · émissions
   │
   ├── tableau de bord web   /           fil d'alertes, sujets, recherche, versions, dépôt, admin
   ├── MCP                   /u/<jeton>/mcp   Claude (plugin clients/claude-plugin) et ChatGPT
   └── bot Telegram          alertes, /suivre, /cherche, dépôt de PDF par transfert
```

## Installation locale

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

## Commandes

```bash
cop fetch                      # collecte tout le catalogue (catalog/sources/*.yaml)
cop fetch --source unfccc_     # un fichier ou un id de source
cop fetch --watched            # seulement les sources surveillées en continu
cop build                      # indexe les documents en attente (cop build --all : tout ré-indexer)
cop embed                      # embeddings manquants (OPENAI_API_KEY requis)
cop load-data                  # données d'émissions (Our World in Data)
cop search "transitioning away from fossil fuels" --level 1
cop stats ; cop sources ; cop log --outcome antibot
cop user-add prenom.nom@exemple.org --name "Prénom Nom"   # lien 24 h (choix du mot de passe) + adresse MCP
cop user-link prenom.nom@exemple.org                        # mot de passe oublié : nouveau lien 24 h
cop programme --jour demain --sujet "water, indigenous"         # où aller (side events, réunions officielles)
cop programme-sync                                               # relève le planner (fait toutes les 6 h par le worker)
cop programme-seors releve.tsv                                   # importe un relevé SEORS (voir plus bas)
cop mail-allow iisd.org carbonbrief.org                          # expéditeurs de lettres admis sur veille@
cop ingest-file note.pdf --title "Note de coordination UE"       # dépôt local (restreint par défaut)
cop serve --port 8000 --with-worker                              # tout en un, en local
cop worker                                                       # veilleur + bot Telegram + digest 7 h
pytest
```

## Niveaux de fiabilité

1 texte adopté · 2 officiel non adopté · 3 science/données · 4 compte rendu de référence ·
5 position d'un acteur · R document interne (accès restreint aux contributeurs).

## Documents internes

Trois façons de les ajouter, tous classés **restreint** par défaut (visibles des seuls membres accrédités,
jamais envoyés à l'API d'embeddings) :
- **Telegram** : transférer le PDF ou le Word au bot (légende = titre ; `#public` pour un document public) ;
- **tableau de bord** : page « Déposer » (fichiers multiples ou URL) ;
- **ligne de commande** : `cop ingest-file`.

Telegram n'est pas chiffré de bout en bout : par défaut, les alertes sur documents internes n'en donnent ni le
titre ni un extrait (`COP_TELEGRAM_RESTRICTED_SNIPPETS=0`).

## Sources protégées par un anti-bot

`unfccc.int` (pages et PDF) répond par un challenge Imperva aux clients automatisés. **Il n'est pas contourné.**
Les documents à cote FCCC sont pris sur le Système de documents officiels de l'ONU
(`documents.un.org`), y compris, pendant la COP, les documents « L » (projets de décision) par énumération
des cotes. Les versions « unedited » publiées seulement sur unfccc.int, et le Daily Programme, se déposent à la
main (Telegram ou tableau de bord) ; `cop log --outcome antibot` liste ce qui a été bloqué.

## Programme (outil MCP `programme`)

- **Planner** (https://unmai-planner-cop31.netlify.app/, non officiel, page publique) : side events, réunions
  officielles du calendrier CCNUCC, ~1 000 événements hors site, résumé des négociations par jour. Relevé toutes les
  6 h par le worker. C'est une copie figée à sa date de construction : les salles peuvent avoir changé.
- **SEORS** (https://seors.unfccc.int/reports/events_list.html), programme officiel des side events, derrière
  l'anti-bot Imperva : jamais relevé par le serveur. Chaque side event renvoyé par l'outil porte le lien SEORS du
  jour. Pour mettre les salles à jour : ouvrir SEORS dans un navigateur, coller `agenda.SEORS_SNIPPET` dans la
  console (le relevé, sans aucune coordonnée personnelle, part dans le presse-papiers), l'enregistrer dans
  `releve.tsv`, puis `cop programme-seors releve.tsv` (dans le conteneur). Le relevé remplace les side events du
  planner et liste les salles et horaires qui ont changé.
- Les consultations informelles par point de l'ordre du jour ne figurent dans aucune des deux sources : seul le
  Daily Programme les donne (dépôt manuel).

## Déploiement sur le VPS (en production depuis le 6 novembre 2026)

- Adresse : **https://cop31.akimx.fr** (tableau de bord `/`, MCP personnel `/u/<jeton>/mcp`), bot **@AssistantCOP31Bot**.
- VPS `debian@<vps>`, dépôt dans `/home/debian/RAG_cop_climat` (même convention que les autres corpus),
  conteneurs `cop31-app-1` (web + MCP, alias `cop31-app` sur le réseau `web`) et `cop31-worker-1` (veille, alertes, bot,
  récapitulatif 7 h), TLS par l'edge Caddy partagé (`/opt/mcp-edge/conf.d/cop31.caddy`).
- Un seul exemplaire du bot peut tourner à la fois : ne pas lancer `cop worker` en local avec le même jeton.
- Comptes invités : lien ou QR d'invitation (`/admin`, `/inviter`), inscription sans mot de passe (nom + e-mail),
  session d'un an. Sur un autre appareil, code à 6 chiffres envoyé par e-mail (et par Telegram si le compte est
  relié), sauf si le compte a choisi un mot de passe. Envoi par un service gratuit, réglé dans `deploy/.env` :
  `COP_MAIL_PROVIDER=resend` (ou `brevo`), `COP_MAIL_API_KEY=…`, `COP_MAIL_FROM="COP31 tracker <cop31@mail.akimx.fr>"`
  (domaine vérifié chez le service, enregistrements DNS qu'il fournit). Sans ces réglages, la connexion par code
  passe seulement par Telegram ; à défaut, un admin génère un lien de connexion 24 h.

Mise à jour du code depuis le Mac :

```bash
rsync -az --exclude data --exclude .venv --exclude .git --exclude __pycache__ ./ debian@<vps>:/home/debian/RAG_cop_climat/
ssh debian@<vps> 'cd RAG_cop_climat && sudo docker compose -f deploy/docker-compose.yml up -d --build'
```

Comptes (le jeton n'est affiché qu'une fois) :

```bash
ssh debian@<vps> 'cd RAG_cop_climat && sudo docker compose -f deploy/docker-compose.yml exec -T app cop user-add prenom.nom@exemple.fr --name "Prénom Nom"'
```

Première installation (pour mémoire) : `deploy/.env` depuis `deploy/.env.example`, `HOST=debian@… bash scripts/transfer-data.sh`,
`mv data/cop.incoming.sqlite3 data/cop.sqlite3`, `sudo bash scripts/deploy.sh`,
`sudo DOMAIN=cop31.akimx.fr bash scripts/install-edge-site.sh`.

### Courrier entrant

MX de `cop31.akimx.fr` → le VPS, port 25 (service `mail`, STARTTLS avec le certificat de Caddy).
`depot-<code>@cop31.akimx.fr` : une adresse secrète par collègue (page Déposer), contenu restreint par
défaut, `[public]` dans l'objet pour le rendre public. `veille@cop31.akimx.fr` : abonnements aux lettres ;
expéditeurs autorisés → niveau 4 public, les autres (et les confirmations d'abonnement) attendent dans
Admin → Courrier. Messages bruts archivés dans `data/mail/archive/`.
