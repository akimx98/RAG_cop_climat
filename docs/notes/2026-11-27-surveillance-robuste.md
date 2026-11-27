# 2026-11-27 — Surveillance robuste : alertes sous verrou, WAL, « Rétabli » à tort, lien de connexion

*Taille : M (alertes Telegram, base, authentification) · Statut : brouillon*
*Fichiers concernés : `monitor.py` (`report`), `watchdog.py` (`issues`, `check`), `db.py` (`connect`),
`auth.py` (`by_login_link`), `tests/test_watchdog.py`, `tests/test_surveillance.py`*
*Issues : #19, #20, #21, #23*

## Problème

L'incident du 26/11 (note 2026-11-26-connexion-sqlite-coincee) a montré quatre faiblesses :

1. **#20 — alertes perdues sous verrou.** `monitor.report` écrit d'abord dans `error_events` ; si la base reste
   verrouillée plus de 30 s, il lève, et l'alerte n'est ni comptée ni envoyée (`monitor.report a échoué`, 26/11).
2. **#21 — signal tardif.** Le WAL a grossi pendant 3 h (303 Mo) et la base principale n'a plus été écrite après
   17:18, bien avant que les battements n'alertent (15 à 90 min). De plus, le fichier WAL ne rétrécit jamais
   (aucun `journal_size_limit`) : 321 Mo occupés sur le disque depuis, alors qu'il est presque vide.
3. **#23 — « Rétabli » puis nouvelle alerte une minute après.** Cause reconstituée : à 19:14 UTC la boucle
   principale du worker était longue (veille), donc le site a pris le relais du chien de garde. La connexion du
   site était coincée sur la version de 17:18 de la base, où `hb:indexation` et `hb:direct` étaient frais : il a
   conclu au rétablissement et **envoyé** « Rétabli » avant d'écrire son état, écriture qui a échoué (verrou). Le
   worker, à 19:15, a revu le problème et alerté de nouveau. La lecture figée est corrigée (ef77254) ; reste
   l'ordre : on annonce avant d'avoir enregistré.
4. **#19 — lien de connexion brûlé.** `auth.by_login_link` valide la consommation du lien (`commit`) avant
   `mark_verified` ; si celui-ci échoue (verrou), le lien est perdu.

## Faits mesurés

| Fait | Valeur | Comment mesuré |
|---|---|---|
| WAL après l'incident | fichier de 321 747 312 octets, inchangé depuis le 26/11 malgré les checkpoints | `ls -la data/`, 27/11 10:18 |
| `journal_size_limit`, `wal_autocheckpoint` | aucun réglage dans `src/` (défauts : pas de limite, 1000 pages) | `grep` |
| Disque | 30 Go / 40, 8,4 Go libres | `df -h`, 27/11 |
| Base principale pendant l'incident | dernière écriture 17:18:39, alertes de battement à partir de 17:59 | note du 26/11 |
| « Rétabli » à tort | 19:14 UTC, alerte suivante 19:15 | captures Telegram, `docker logs` |
| `monitor.report` sous verrou | « monitor.report a échoué », plusieurs fois par minute de 17:18 à 19:29 | `docker logs` du 26/11 |

## Modes de panne

| Composant | Panne possible | Aujourd'hui | Après |
|---|---|---|---|
| Alerte pendant un verrou long | perdue | perdue (journal seulement) | envoyée quand même, au plus une par erreur et par 30 min (mémoire du processus), destinataires du dernier relevé réussi |
| Lecteur coincé (version figée) | WAL sans fin, disque | vu par les battements au bout de 15–90 min | alerte « reprise du WAL arrêtée » dès 20 min |
| WAL après un gros lot d'écritures | fichier gardé à sa taille maximale | 321 Mo perdus | ramené à 64 Mo au checkpoint suivant |
| Chien de garde qui lit une base figée | « Rétabli » à tort | envoyé, puis nouvelle alerte | l'état est enregistré **avant** l'annonce ; si l'écriture échoue, rien n'est annoncé |
| Lien de connexion | brûlé si `mark_verified` échoue | lien perdu | consommation et vérification dans une seule transaction |

## Critères d'acceptation

1. `monitor.report` : si l'écriture dans `error_events` échoue, l'alerte part quand même (Telegram, à défaut
   e-mail) vers les administrateurs du dernier relevé réussi, au plus une fois par clé d'erreur et par
   `RESEND_MINUTES` dans le processus ; la fonction ne lève toujours jamais (test : base verrouillée simulée).
2. Le message de repli ne contient rien de plus que le message normal (lieu, type, début du message d'erreur).
3. `db.connect` règle `PRAGMA journal_size_limit` à 64 Mo (test : la valeur lue sur une connexion).
4. `watchdog.issues` signale en **bloquant** « reprise du WAL arrêtée » quand plus de 128 Mo de pages du WAL restent
   non reportées dans la base après un checkpoint passif (`PRAGMA wal_checkpoint(PASSIVE)` : `log − checkpointed`) ;
   rien quand un gros fichier WAL est déjà reporté (test avec un vrai lecteur qui tient une ancienne version).
5. `watchdog.check` enregistre l'état avant d'envoyer les « Rétabli » ; si l'enregistrement échoue, aucun
   « Rétabli » ne part (test).
6. `auth.by_login_link` : si la vérification de l'adresse échoue, le lien n'est pas consommé (test).
7. Suite complète verte ; après déploiement : WAL ramené à ≤ 64 Mo, aucune alerte nouvelle à tort.

## Conception

- #20 : dans `report`, la comptabilité en base est dans un `try` ; en cas d'échec, repli en mémoire
  (`_mem_sent`, sous verrou) et envoi direct. Les destinataires sont mis en cache à chaque lecture réussie
  (`admin_chats`, `admin_emails`) ; si la lecture échoue aussi, on prend le cache. Écarté : un fichier d'attente
  sur disque (complexité, et l'alerte doit partir tout de suite).
- #21 : `journal_size_limit` = 64 Mo dans `db.connect` (le fichier WAL est ramené à 64 Mo quand il repart au
  début) ; contrôle dans `issues()` par un checkpoint passif, qui renvoie les pages du WAL et celles reportées : la
  différence est ce qu'un lecteur empêche de reporter. Seuil : 128 Mo. Première version (taille du fichier WAL et
  date de la base) écartée en revue : le fichier ne rétrécit pas toujours et la date de la base ne change qu'aux
  checkpoints, d'où de fausses alertes.
- #23 : `check` calcule le nouvel état, l'écrit, puis envoie les « Rétabli ».
- #19 : `INSERT OR IGNORE` + `UPDATE users` puis un seul `commit` ; un échec annule les deux (db.connect annule
  sur verrou).

Hors périmètre : #22 (gel de l'hôte), sonde externe (#1).

## Déploiement

`scripts/ship.sh`. Au premier checkpoint après le démarrage, le WAL est tronqué à 64 Mo. Retour arrière :
`scripts/ship.sh --rollback`.

## Revue

Tour 1 (`reviewer`) : **non bloquant**, critères 1 à 6 conformes au texte, 228 tests verts. Constats acceptés :
1. (important) le contrôle du WAL par taille de fichier et date de la base aurait alerté à tort (fichier de 321 Mo
   déjà reporté ; base non modifiée entre deux checkpoints en période calme) → mesure directe des pages en attente
   (critère 4 réécrit, test avec un vrai lecteur coincé) ;
2. le repli sans destinataires connus (juste après un démarrage) notait l'erreur comme envoyée et la masquait
   30 min → rien n'est noté sans destinataires ; les destinataires sont relus à chaque vérification du chien de
   garde (site et worker, 5 min) ; test.
Reportés en suites : 3. rafale possible au retour de la base (le repli et `error_events` ne se connaissent pas ;
une mémoire par processus) et `_mem_sent` jamais purgé ; 5. « Dégradé » renvoyé à chaque vérification si l'état ne
peut pas être écrit (existant). Constat 6 (`web.login_link` : 500 si la base est verrouillée, le lien reste valide) :
accepté tel quel.

Tour 2 (`reviewer`, nouveau) : **non bloquant**, 231 tests verts ; sémantique du checkpoint passif mesurée par le
relecteur (repos `(0, 15, 15)`, lecteur coincé `log − checkpointed > 0`, hors WAL `(0, -1, -1)` filtré). Constats
mineurs corrigés : pas de checkpoint sur une connexion en transaction (il lèverait SQLITE_LOCKED et `db._Connexion`
annulerait la transaction de l'appelant) ; destinataires du repli amorcés aussi dans le site (l'amorce était après
la sortie anticipée du site). Reporté : exiger deux vérifications consécutives avant l'alerte WAL si une fausse
alerte apparaît (lecteur long légitime + plus de 128 Mo d'écritures concurrentes : jamais observé).

## Suites
