# COP Tracker — COP31

Corpus vérifiable, veille en temps réel et alertes pour suivre les négociations de la COP31
(Antalya, 9–20 novembre 2026). Projet indépendant, gratuit et non commercial, sans lien avec la CCNUCC
ni avec une délégation. Code sous licence MIT ([LICENSE](LICENSE)).

**Documentation de référence : [docs/architecture.md](docs/architecture.md)** (niveaux de fiabilité, données,
outils MCP, skills, tâches planifiées, réglages, confidentialité, coûts). Ce README couvre l'installation, les
commandes et l'exploitation.

```
sources (ODS/ONU, EUR-Lex, Conseil UE, GIEC, PNUE, ENB, Carbon Brief, OWID…)
   │  cop fetch / veilleur (worker)       reçus SHA-256, versions, anti-bot respecté
   ▼
data/cop.sqlite3  documents · passages (FTS5) · vecteurs · utilisateurs · sujets · alertes · émissions
   │
   ├── site web              /           connecter un LLM, planifier ma journée, mes sujets, à propos, base de données
   ├── MCP                   /u/<jeton>/mcp   Claude (plugin clients/claude-plugin), ChatGPT, Le Chat
   └── bot Telegram          alertes sur mes sujets, récapitulatif sans IA 7 h et 19 h, /cherche
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
cop soumissions etat|index|telecharger|analyser|comparer|appels|appel|rattacher|dossiers   # soumissions CCNUCC
cop groupes --sync ; cop groupes --groupe LDC ; cop groupes --partie China                 # groupes de négociation
cop pavillons-sync ; cop pavillons --sujet "rainforest, Amazon"                           # programmes des pavillons
cop programme --jour demain --sujet "water, indigenous"         # où aller (side events, réunions officielles)
cop programme-sync                                               # relève le planner (fait toutes les 6 h par le worker)
cop programme-seors releve.tsv                                   # importe un relevé SEORS (voir plus bas)
cop mail-allow iisd.org carbonbrief.org                          # expéditeurs de lettres admis sur veille@
cop lettres-relire [--essai]       # relit les lettres archivées (texte complet, date) et remplace leur texte sur place
cop enb annoter|couverture         # en-têtes des PDF de l'ENB ; liens de chaque lettre ENB couverts ou non par un PDF
cop reextraire --source twn-%      # réextrait des documents (colonnes des PDF, encodage), même identifiant
cop recuperer --url presscorner/detail --vides   # retélécharge des documents entrés vides (discours de la Commission)
cop doublons --calculer            # empreintes manquantes et doublons de contenu (même texte, deux fichiers)
cop ingest-file note.pdf --title "Note de coordination UE"       # dépôt local (restreint par défaut)
cop serve --port 8000 --with-worker                              # tout en un, en local
cop worker                                                       # veilleur + bot Telegram + récapitulatif 7 h et 19 h
pytest
```

## Niveaux de fiabilité, sources, programme

Voir la [documentation de référence](docs/architecture.md) (§2 niveaux, §3 données et sources, §6 tâches).

## Documents internes

Le dépôt de documents par les contributeurs (site, Telegram, adresse `depot-<code>@`) et les notes de terrain sont
retirés. Les documents internes déjà présents restent **restreints** (visibles des seuls comptes autorisés, jamais
envoyés à l'API d'embeddings, masqués dans Telegram). Un administrateur peut encore ajouter un fichier en ligne de
commande (`cop ingest-file`) ou déposer le PDF d'un texte de session bloqué par un anti-bot (base de données, onglet
« Session texts »).

## Sources protégées par un anti-bot

Jamais contournées (`unfccc.int`, SEORS, page « Party groupings » de la CCNUCC). Les documents à cote FCCC passent
par l'ODS de l'ONU ; le reste se dépose à la main (Telegram ou tableau de bord) ; `cop log --outcome antibot` liste
ce qui a été bloqué. SEORS : bouton « Relevé SEORS → COP31 » (Admin → Programme) à cliquer sur la page SEORS
ouverte dans son navigateur, ou relevé automatique horaire dès que la CCNUCC autorise l'adresse du serveur. ENB : les pages HTML sont bloquées mais les PDF quotidiens passent (source `enb-daily-pdf`).
Mode dégradé : version PDF de la même page, `deposer_url` d'une autre adresse publique, sinon dépôt manuel. Relevé SEORS des salles des side events : ouvrir SEORS dans un navigateur, coller
`agenda.SEORS_SNIPPET` dans la console, enregistrer le presse-papiers dans `releve.tsv`, puis
`cop programme-seors releve.tsv` (dans le conteneur).

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
Seule adresse acceptée : `veille@cop31.akimx.fr`, abonnements aux lettres (le dépôt par `depot-<code>@` est retiré) ;
expéditeurs autorisés → niveau 4 public, les autres (et les confirmations d'abonnement) attendent dans
Admin → Courrier. Messages bruts archivés dans `data/mail/archive/`.
