# 2026-11-25 — Passage au domaine opencop.org

*Taille : M · Statut : revu (tour 1), à valider*
*Fichiers concernés : `deploy/edge/sites/cop31.caddy.template`, `scripts/install-edge-site.sh`, `scripts/ship.sh`,
`deploy/.env.example`, `deploy/.env` (VPS), `README.md`, `.claude-plugin/marketplace.json`*

## Problème

Le site devient OpenCOP (commit a93f861) et doit servir toutes les COP : l'adresse `cop31.akimx.fr` ne
convient plus. Le domaine visé est `opencop.org` (pas encore acheté). Contrainte : la zone DNS `akimx.fr` (OVH)
porte d'autres services et ne doit pas bouger, et ce qui pointe déjà vers `cop31.akimx.fr` doit continuer à
marcher.

## Faits mesurés

| Fait | Valeur | Comment mesuré |
|---|---|---|
| `cop31.akimx.fr` | A 152.228.141.68, pas d'AAAA, MX `10 cop31.akimx.fr.`, pas de TXT | `dig`, 25/11 |
| DNS d'`akimx.fr` | OVH (`ns200.anycast.me`, `dns200.anycast.me`) | `dig NS`, 25/11 |
| `opencop.org` | aucun NS publié (probablement libre, **non vérifié** au registre) | `dig NS`, 25/11 |
| Connecteurs MCP | adresse personnelle `PUBLIC_BASE/u/<jeton>/mcp`, collée dans les clients des utilisateurs | `server.py:24`, `cli.py:98`, `web.py:1063` |
| Contrôle d'hôte MCP | `MCP_ALLOWED_HOSTS` (liste) → hôtes et origines admis ; vide = contrôle coupé | `mcp_server.py:58` |
| Contrôle d'origine des formulaires | accepte `PUBLIC_BASE` **ou** l'hôte de la requête elle-même | `web.py:185-194`, `web.py:1297` |
| Cookies (session, jeton, langue, démo) | sans attribut `Domain` → liés à l'hôte ; `secure` si `PUBLIC_BASE` en https | `web.py:159, 251-257, 644, 1170` |
| Liens signés (`/entrer/`, `/fichier/`, `/join/`) | signature indépendante de l'hôte | `auth.py:294-339` |
| `/entrer/<lien>` | GET affiche un bouton, seul le POST (même origine) connecte | `web.py:425-441` |
| Liens sortants (alertes, Telegram, récapitulatifs, outils MCP, e-mails de code) | tous construits avec `PUBLIC_BASE` | `grep PUBLIC_BASE src/` |
| Courrier entrant | adresse `veille@MAIL_DOMAIN` ; `MAIL_DOMAIN` = `COP_MAIL_DOMAIN`, sinon **déduit de `PUBLIC_BASE`** ; certificat lu dans le stockage de Caddy pour ce domaine | `config.py:27-31`, `mailin.py:68-74` |
| Route de l'edge | gabarit à un `__DOMAIN__`, substitué par `sed` ; validation de toute la configuration avant écriture | `install-edge-site.sh` |
| Domaine de la route | `DOMAIN` obligatoire à chaque lancement, sinon lu dans `deploy/edge/.env` (absent du dépôt ; présence sur le VPS non vérifiée) : la liste des domaines n'est mémorisée nulle part | `install-edge-site.sh:22-35` |
| Hôte vu par l'application | `proxy_headers=True` et `Host` conservé par `reverse_proxy` : `request.url.netloc` = hôte réel | `server.py:137` |
| Contrôle après déploiement | `URL` par défaut `https://cop31.akimx.fr` | `ship.sh:16` |
| Bot Telegram | lecture par interrogation (un seul lecteur), pas de webhook : indépendant du domaine | CLAUDE.md, `telegram.py` |
| SSH vers le VPS | bloqué depuis certains réseaux (port 22) ; dépannage : partage de connexion | `mcp-edge/CONVENTIONS.md`, 25/11 |

Hypothèses non vérifiées (à vérifier avant de coder dessus) :

- H1. Valeurs actuelles de `deploy/.env` sur le VPS (`COP_PUBLIC_BASE`, `MCP_ALLOWED_HOSTS`, `COP_MAIL_DOMAIN`,
  `COP_MAIL_FROM`) : non relues (SSH bloqué le 25/11). À lire avant toute modification.
- H2. Plusieurs adresses séparées par des virgules sur la ligne de site : syntaxe Caddy standard, passée telle
  quelle par le `sed`. Confirmée sans risque par `caddy validate` dans le script (refus = rien n'est écrit).
- H3 (**critique**). Le certificat de `cop31.akimx.fr` reste renouvelé tant que cet hôte figure dans la route :
  c'est lui que lit le service `mail` pour STARTTLS (`docker-compose.yml:43-57`). Toute réinstallation de la route
  doit garder ce nom ; vérifier aussi que le fichier certificat existe dans le volume avant de toucher `.env`.
- H4. Le DNS d'opencop.org chez le bureau d'enregistrement peut contenir des AAAA ou CAA hérités (page de parking) :
  un AAAA mal pointé fait échouer l'ACME par intermittence.

## Modes de panne

| Composant | Panne possible | Ce qu'on en voit aujourd'hui | Après le changement |
|---|---|---|---|
| Connecteurs MCP | ancien hôte refusé (`MCP_ALLOWED_HOSTS` sans lui) ou redirigé (clients qui ne suivent pas les 30x en POST) | rien : erreurs côté client de l'utilisateur | les deux hôtes admis ; `/u/` jamais redirigé ; vérifié par critère 3 |
| Courrier entrant | `MAIL_DOMAIN` passe à `opencop.org` : `veille@cop31.akimx.fr` refusé, certificat introuvable | lettres perdues sans bruit | `COP_MAIL_DOMAIN=cop31.akimx.fr` fixé explicitement ; critère 5 |
| Certificat | route installée avant que le DNS d'opencop.org pointe vers le VPS : échecs ACME répétés, limites Let's Encrypt | journaux de l'edge | ordre imposé : DNS vérifié par `dig` avant la route |
| Edge partagé | erreur de syntaxe : rechargement refusé pour tous les services | validation du script | inchangé (validation complète avant écriture) |
| Sessions | utilisateurs connectés sur l'ancien hôte, liens des alertes vers le nouveau : reconnexion | — | attendu, une seule fois par utilisateur ; pas de message dédié (hors périmètre) |
| Contrôle après déploiement | `ship.sh` teste encore l'ancien hôte et passe alors que le nouveau est cassé | — | `URL` par défaut sur opencop.org ; l'ancien testé aussi (critère 7) |
| Redirection (phase B) | boucle, ou redirection de `/u/`, `/healthz`, `/admin/seors/import` | — | exclusions explicites ; critère 9 |
| Réinstallation de la route | relancée avec un seul domaine (phase B, retour arrière, autre intervention) : l'autre domaine n'est plus servi ni renouvelé ; si c'est `cop31.akimx.fr`, le courrier entrant perd son certificat | — | commande complète écrite ici et `DOMAIN` mémorisé dans `deploy/edge/.env` sur le VPS ; critère 11 |
| Formulaires ouverts à la bascule (phase B) | POST d'une page affichée sur l'ancien hôte, redirigé en 308 : `Sec-Fetch-Site: cross-site` → 403 « rechargez la page » (`web.py:185-200`, `/try/ask` : `web.py:1297`) | — | risque accepté (aucune perte de données, une fois) ; comportement constaté au critère 9 |
| Retour arrière | liens envoyés pendant la phase A (alertes, e-mails, sorties MCP) vers opencop.org, qui n'est plus routé | — | conséquence acceptée ; préférer garder opencop.org dans la route et ne remettre que `PUBLIC_BASE` |

## Critères d'acceptation

Phase A — les deux domaines servent le site :

1. `https://opencop.org/healthz` et `https://cop31.akimx.fr/healthz` répondent 200, chacun avec un certificat
   valide pour son nom.
2. Les liens sortants nouveaux (e-mail de code, alerte Telegram, récapitulatif, outil MCP) utilisent
   `https://opencop.org`.
3. Un connecteur MCP existant (`https://cop31.akimx.fr/u/<jeton>/mcp`) répond toujours ; le même jeton fonctionne
   sur `https://opencop.org/u/<jeton>/mcp`. Un hôte non listé est refusé.
4. Connexion par code e-mail réussie sur chacun des deux domaines (contrôle d'origine, cookie `secure`).
5. Le courrier entrant reste sur `veille@cop31.akimx.fr` : le service `mail` démarre, STARTTLS présente le
   certificat de `cop31.akimx.fr`, une lettre de test arrive dans `/admin/courrier`.
6. La zone DNS `akimx.fr` n'est pas modifiée.
7. `scripts/ship.sh` contrôle `opencop.org` par défaut et vérifie aussi `/healthz` sur `cop31.akimx.fr`.
8. Tests verts ; `grep -rn "akimx.fr" src/` ne renvoie rien. Exceptions admises ailleurs : `README.md`,
   `.claude-plugin/marketplace.json`, `tests/test_qr.py` (donnée de test), `scripts/ship.sh`, `deploy/.env.example`.

Phase B — redirection (au moins quelques jours après A) :

9. Sur `cop31.akimx.fr`, toute requête est redirigée (308, chemin et requête conservés) vers `opencop.org`, **sauf**
   `/u/*`, `/mcp*`, `/healthz*`, `/admin/seors/import` (servis directement). Pas de boucle sur `opencop.org`.
   Vérifié par méthode : `curl -X POST …/u/<jeton>/mcp` et `curl -X OPTIONS …/admin/seors/import` → réponse de
   l'application (200/204/401/403), jamais 30x ; `curl -I` sur `/`, `/static/<fichier>`, `/doc/1?x=y` → 308 avec
   `Location: https://opencop.org/…` exact ; `curl -X POST …/signin` sur l'ancien hôte → 308, puis comportement
   noté (403 attendu, accepté).
10. Un lien `/entrer/…`, `/join/…` ou `/fichier/…?exp=…&sig=…` reçu sur l'ancien domaine fonctionne après
    redirection (signatures indépendantes de l'hôte ; la connexion se fait sur `opencop.org`).
11. Après **chaque** réinstallation de la route (A, B, retour arrière) : `/healthz` répond 200 sur les deux hôtes,
    et le critère 5 (STARTTLS sur le port 25 avec le certificat de `cop31.akimx.fr`, lettre de test reçue) est
    revérifié.

## Conception

Options envisagées :

- **Changer d'hôte d'un coup** (couper `cop31.akimx.fr`) : casse tous les connecteurs MCP, les QR codes imprimés,
  les liens déjà envoyés et le courrier entrant. Écarté.
- **Redirection immédiate de tout l'ancien hôte** : les clients MCP ne suivent pas tous une redirection en POST.
  Écarté pour `/u/` et `/mcp`.
- **Retenu : ajouter, puis rediriger.** Phase A : une seule route Caddy pour les deux noms (même backend),
  `PUBLIC_BASE` sur opencop.org, `MCP_ALLOWED_HOSTS` avec les deux, `COP_MAIL_DOMAIN` figé sur l'ancien domaine.
  Phase B : dans la même route, un matcher sur l'ancien hôte hors exclusions, puis la redirection :

  ```
  @ancien {
  	host cop31.akimx.fr
  	not path /u/* /mcp* /healthz* /admin/seors/import
  }
  redir @ancien https://opencop.org{uri} 308
  ```

  `redir` passe avant `reverse_proxy` dans l'ordre par défaut des directives Caddy ; à confirmer par
  `caddy validate` puis par le critère 9 (non vérifié à ce stade). Aucun changement de code Python n'est nécessaire (le contrôle d'origine accepte déjà l'hôte de la requête).

`www.opencop.org` : à ajouter dans la route et le DNS seulement si on le veut (redirigé vers `opencop.org` en
phase B, même règle).

Hors périmètre :

- Migrer le courrier entrant vers `@opencop.org` (abonnements aux lettres à refaire chez chaque émetteur).
- Envoi des e-mails depuis un domaine opencop.org vérifié chez le service d'envoi (SPF, DKIM).
- Renommer le serveur MCP, le plugin et les instructions clients (`cop31`, « COP31 Tracker »), le dépôt GitHub.
- Avertir les utilisateurs du changement (e-mail ou message Telegram).
- `opencop.fr`.

## Déploiement

Phase A, dans l'ordre :

1. Acheter `opencop.org` ; créer `A opencop.org → 152.228.141.68` (et `www` si voulu). Vérifier depuis le Mac
   **avant** l'étape 3 : `dig +short opencop.org A` (= l'IP du VPS), `dig +short opencop.org AAAA` (vide),
   `dig +short opencop.org CAA` (vide ou autorisant `letsencrypt.org`).
2. Sur le VPS (après accord) : relire `deploy/.env` (H1) et `deploy/edge/.env` s'il existe ; vérifier la présence
   du certificat de `cop31.akimx.fr` dans le volume de Caddy (H3) ; sauvegarder `deploy/.env` en
   `deploy/.env.avant-opencop`, puis
   `COP_PUBLIC_BASE=https://opencop.org`, `MCP_ALLOWED_HOSTS=opencop.org,cop31.akimx.fr`,
   `COP_MAIL_DOMAIN=cop31.akimx.fr`.
3. Mémoriser la liste des domaines : `DOMAIN=opencop.org, cop31.akimx.fr` dans `deploy/edge/.env` sur le VPS
   (fichier propre au VPS ; `ship.sh` synchronise sans `--delete` et le dépôt n'en contient pas). Puis
   `sudo -E DOMAIN="opencop.org, cop31.akimx.fr" bash scripts/install-edge-site.sh` : toujours la liste complète,
   même si le fichier existe (validation, rechargement à chaud, sans coupure pour les voisins).
4. Commiter les changements du dépôt (`ship.sh`, `.env.example`, doc), puis `scripts/ship.sh` (recrée les trois
   conteneurs avec le nouveau `.env`).
5. Vérifications : critères 1 à 5 et 11 (`curl -sI`, `openssl s_client -connect cop31.akimx.fr:25 -starttls smtp`,
   une connexion par code sur chaque domaine, un appel MCP par chaque hôte).

Retour arrière de la phase A (sans `ship.sh`, qui exige un arbre commité, refait les tests et contrôlerait
opencop.org) :

```
ssh … 'cd RAG_cop_climat && cp deploy/.env.avant-opencop deploy/.env && sudo docker compose -f deploy/docker-compose.yml up -d'
```

La route peut garder les deux domaines (rien ne casse côté courrier, et les liens déjà envoyés vers opencop.org
restent valides). Si elle doit revenir à un seul nom : `DOMAIN=cop31.akimx.fr` dans `deploy/edge/.env` et
réinstallation, en sachant que ces liens deviennent morts. Contrôle : `COP_URL=https://cop31.akimx.fr` pour tout
`ship.sh` ultérieur, puis critère 11.

Phase B : modifier le gabarit (matcher ci-dessus), réinstaller la route avec la liste complète ; vérifier critères
9, 10 et 11. Retour arrière : réinstaller le gabarit de la phase A, même liste de domaines, puis critère 11.

À mettre à jour dans d'autres dépôts : inventaire de `mcp-edge/CONVENTIONS.md` (domaine du service).

## Revue

Tour 1 (reviewer, 25/11) : à reprendre, aucun bloquant. Constats acceptés et reportés :
- liste des domaines non mémorisée par `install-edge-site.sh` → étape 3 du déploiement, mode de panne, critère 11 ;
- POST de formulaires ouverts redirigés → 403 : risque accepté, constaté au critère 9 ;
- critère 9 vérifié par méthode (POST, OPTIONS, `Location` exact) ; matcher Caddy écrit dans la conception ;
- retour arrière : commande exacte sans `ship.sh`, `COP_URL` pour l'ancien hôte ;
- courrier revérifié après chaque réinstallation (critère 11), H3 marquée critique, certificat vérifié avant ;
- liens morts après retour arrière : conséquence acceptée, route gardée à deux noms de préférence ;
- AAAA et CAA d'opencop.org vérifiés à l'étape 1 (H4) ;
- critère 8 rendu vérifiable (`grep`, exceptions listées) ;
- H2 vérifiée par `caddy validate` ; numéros de ligne des cookies corrigés ;
- `docs/architecture.md` §7 (`COP_MAIL_DOMAIN`, `MCP_ALLOWED_HOSTS`, `COP_PUBLIC_BASE`) à mettre à jour à la
  livraison (ci-dessous).

Constats refusés : aucun. Tour 2 : seulement si la note change de conception.

## Suites

- Courrier entrant et sortant sur opencop.org.
- Renommage MCP / plugin / instructions clients / dépôt.
- Inventaire `mcp-edge`.

## Promotion (à la livraison)

- [ ] pièges → PITFALLS (route réinstallée avec un seul domaine) ; faits → architecture §7 (valeurs de production
      de `COP_PUBLIC_BASE`, `MCP_ALLOWED_HOSTS`, `COP_MAIL_DOMAIN` figé), README (adresse, MX) ; inventaire
      `mcp-edge/CONVENTIONS.md`
- [ ] suites → issues `suite`
- [ ] statut « livré » ici et dans l'index
