# 2026-11-26 — Connexion SQLite coincée après un « database is locked »

*Taille : incident, puis M (base, boucles du worker et du site) · Statut : brouillon*
*Fichiers concernés : `db.py` (`connect`), `tests/test_core.py` ou nouveau `tests/test_verrou.py` ; lus pour
vérification : `worker.py`, `live_worker.py`, `watchdog.py`, `monitor.py`, `ingest.py` (`run_source`),
`mailin.py`*

## Problème

Le 26/11, des alertes Telegram « chien de garde » arrivent de 18h59 à 20h15 (heure de Paris) : boucles `app`,
`indexation` et `direct` arrêtées, mémoire du worker à 94 % de sa limite. Cause : un gel du VPS a provoqué des
« database is locked ». Une connexion Python dont l'écriture échoue sur ce verrou **reste dans une transaction
ouverte**. Le tour suivant y lit une version figée de la base, et **toute écriture ultérieure sur cette connexion
échoue immédiatement, sans fin** (`SQLITE_BUSY_SNAPSHOT` : écrire depuis une version périmée est impossible ;
le délai `busy_timeout` n'y change rien). Personne n'appelle `rollback()` : `monitor.report` et `watchdog.beat`
réessaient d'écrire sur la même connexion coincée. La version figée empêche aussi de reporter le WAL dans la base,
qui grossit sans limite.

Chronologie (UTC ; Telegram affiche l'heure de Paris, +1 h) :

| Heure | Fait | Source |
|---|---|---|
| 17:08:59–17:13:14 | `rcu_preempt detected stalls`, `kthread starved for 5423 jiffies`, noyau `Tainted: SOFTLOCKUP` ; processeurs à l'arrêt dans `pv_native_safe_halt` | `journalctl -k` |
| 17:11:41 | délais dépassés sur les sondes de santé Docker | `journalctl` (dockerd) |
| 17:16:34 | dernier battement `hb:indexation` avant le redémarrage | `meta` |
| 17:18:38 | premier `sqlite3.OperationalError: database is locked` (collecte, direct, `monitor.report`, battements) | `docker logs cop31-worker-1` |
| 17:18:39 | dernière écriture de `cop.sqlite3` (le WAL n'est plus reporté dans la base ensuite) | `ls --time-style=full-iso data/` |
| ~19:29 | la boucle `direct` sort seule de l'erreur | `docker logs` (plus d'erreur « textes en direct » après 19:29:09) |
| 20:11:24 | redéploiement de 92cfbd2 (autre session) : le site redémarre, `hb:app` repart | `.deployed`, `journalctl` sudo |
| 20:14 | `indexation` toujours en échec toutes les 15 s ; WAL 303 Mo | `docker logs`, `ls` |
| 20:25:27 | **mitigation** : `docker compose restart worker` (accord de l'utilisateur) | — |
| 20:26:27 | `hb:indexation` écrit de nouveau ; aucune erreur depuis | `meta`, `docker logs` |

## Faits mesurés

| Fait | Valeur | Comment mesuré |
|---|---|---|
| Gel du VPS | ~4 min (17:09–17:13), processeurs inactifs privés de temps de calcul | `journalctl -k`, 26/11 |
| Vol de temps de calcul après l'incident | 0,0 % `st` | `top -bn2`, 20:12 |
| Erreurs « database is locked » dans le worker | 4 à 7 par minute de 17:18 à 20:13, puis 0 après le redémarrage | `docker logs -t … \| grep locked \| cut -c1-16 \| uniq -c` |
| Écritures des autres fils pendant ce temps | réussies (`hb:courrier`, `hb:soumissions`, `hb:direct` à jour à 20:14) | `meta` en lecture seule |
| WAL | 303 Mo à 20:14, base principale figée à 17:18:39 ; reportée dans la base à 20:25 | `ls -la data/` |
| Verrous de lecture sur le `-shm` | le worker tient la marque de lecture 125 (lecteur durable) ; aucun verrou d'écriture (octet 120) sur 20 échantillons | `/proc/locks`, 20:13 |
| Mémoire du worker | 568 / 600 Mo à 19:15 (alerte) ; 117 Mo après le redémarrage | alerte Telegram ; `docker stats` |
| Mécanisme | reproduit : après un échec, `in_transaction=True` ; trois écritures suivantes échouent ; un `rollback()` débloque | script local (Python 3.13.2, SQLite 3.43.2) ; production : SQLite 3.46.1 |
| `rollback()` dans `src/` | aucun | `grep -n rollback src/cop_corpus/*.py` |
| Curseurs explicites | aucun `.cursor()` dans `src/` ; tout passe par `conn.execute`, `executemany`, `executescript` | `grep` |
| `except sqlite3.OperationalError` existants | 4 (`alerts.py:160`, `search.py:336,404` : syntaxe FTS ; `live.py:93` : migration après `commit`) ; aucun ne vise le verrou | `grep` |

Hypothèses non vérifiées :

- **H1** : la boucle `app` (fil du chien de garde du site, battement figé de 17:10 environ à 20:11) était coincée
  par le même mécanisme. Non vérifiable : les journaux de l'ancien conteneur ont disparu avec lui au redéploiement.
- **H2** : `direct` est sortie seule vers 19:30 parce qu'un `executescript` (schémas d'`agenda`, `pavillons`,
  `groupes`, qui tournent toutes les 6 h ou 24 h et ont tourné à 19:30) envoie d'abord un `COMMIT`, ce qui ferme la
  transaction coincée. Cohérent avec les journaux, non démontré.
- **H3** : la mémoire du worker a monté à cause de la version figée et du WAL qui grossit. Corrélation seulement.
- **H4** : le gel vient de l'hôte OpenStack (OVH), pas d'une charge de la machine virtuelle. Non vérifié côté OVH.
- **H5** : dans le site, les connexions par fil (`db.get()`, pool de fils depuis 92cfbd2) peuvent se coincer de la
  même façon ; une page servie par ce fil renverrait alors une erreur à chaque écriture. Non observé (journaux
  perdus), mais le code le permet.

## Modes de panne

| Composant | Panne possible | Ce qu'on en voit aujourd'hui | Après le changement |
|---|---|---|---|
| Toute connexion longue (boucles du worker, chien de garde, courrier, connexions par fil du site) | écriture en échec sur verrou → transaction laissée ouverte → échecs sans fin | battement figé, alerte au bout de 15 à 90 min, WAL qui grossit ; seul un redémarrage débloque | la transaction est annulée au premier échec ; le tour suivant repart sur une version fraîche |
| Fonction qui rattrape l'exception et continue dans la même transaction (`run_source` par URL, `mailin` par lettre) | le `rollback` annule aussi les écritures non validées des éléments précédents | aujourd'hui elles restaient bloquées puis perdues de toute façon | perdues tout de suite, retentées au passage suivant ; **à vérifier** : aucune écriture partielle qui laisse un état incohérent (critère 5) |
| Migration qui rattrape une `OperationalError` (colonne déjà là) | un `rollback` annulerait les migrations précédentes non validées | — | non concerné : seule la famille « verrou » (`SQLITE_BUSY`, `SQLITE_LOCKED`) déclenche le `rollback` |
| Gel du VPS lui-même | blocage de quelques minutes | voir plus haut | inchangé (hors périmètre) |
| `monitor.report` pendant un verrou | l'alerte n'est pas enregistrée (`monitor.report a échoué`) | alertes perdues ou en retard | inchangé si le verrou dure plus de 30 s ; plus d'échec permanent |

## Critères d'acceptation

1. Un test reproduit l'incident sur une base WAL temporaire, avec deux connexions venant de `db.connect()` : une
   écriture échoue sur verrou, une autre connexion écrit, puis la première réécrit. **Échoue sans la correction**
   (« database is locked » au deuxième essai) et **passe avec**.
2. Après une `OperationalError` de la famille verrou (`exc.sqlite_errorcode & 0xFF` dans `SQLITE_BUSY`,
   `SQLITE_LOCKED`) levée par `execute`, `executemany`, `executescript` ou `commit` d'une connexion de
   `db.connect()`, `conn.in_transaction` est `False` et l'exception d'origine remonte inchangée.
3. Les autres `OperationalError` (syntaxe FTS, colonne en double, table absente) **ne** déclenchent **pas** de
   `rollback` : test qui vérifie qu'une écriture non validée survit à une erreur de syntaxe FTS rattrapée.
4. Le correctif est à un seul endroit (`db.connect`), couvre aussi `db.get()` et le conteneur `mail`, et ne change
   ni le niveau d'isolation, ni `busy_timeout`, ni le mode WAL.
5. Relecture écrite (dans cette note, § Conception) des boucles qui rattrapent une exception puis continuent sur la
   même connexion (`ingest.run_source`, `mailin`, `live_worker.tick`, `soumissions.sync_index`) : pour chacune,
   dire si un `rollback` en cours de route peut laisser un état incohérent. Tout cas incohérent trouvé bloque la
   livraison et revient à la conception.
6. Suite de tests complète verte ; `scripts/ship.sh` sans erreur.
7. Après déploiement : 0 « database is locked » persistant ; battements des 6 boucles du worker et des 2 du site à
   jour (commandes au § Déploiement).

## Conception

Options :

- **A. `rollback()` dans chaque gestionnaire d'erreur** (une dizaine de boucles, plus `monitor.report` et
  `watchdog.beat`). Oublis probables (connexions par fil du site, futures boucles), et `monitor.report(conn=…)`
  passe la connexion d'un autre fil.
- **B. Retenue : sous-classe de `sqlite3.Connection` passée par `factory=` dans `db.connect`.** Elle surcharge
  `execute`, `executemany`, `executescript` et `commit` : sur `OperationalError` de la famille verrou, si
  `in_transaction`, appeler `rollback()` (en ignorant une erreur du `rollback` lui-même), puis relancer l'exception
  d'origine. Un seul endroit, toutes les connexions couvertes. Après un échec sur verrou, garder la transaction
  n'apporte rien : personne ne réessaie dans la foulée, et si la version est périmée l'écriture ne peut plus
  réussir.
- C. Passer en `autocommit=True` (Python 3.12+) : change la sémantique de toutes les écritures du projet. Trop large.

Un `rollback` ne touche que la connexion qui a échoué : les autres fils et processus ne sont pas concernés.
Une lecture (`SELECT`) ne lève pas « locked » en WAL hors récupération ; si elle le faisait, l'annulation serait
sans conséquence.

Relecture du critère 5 (27/11) :

- `soumissions.sync_index`, `live_worker.tick` (`live.sync_*`) : un seul `commit` en fin de passage ; une
  annulation en cours de route jette le passage entier, refait au suivant. Cohérent.
- `ingest.run_source` → `fetch_one` : `_touch_url` valide à chaque URL ; `_index_document` supprime puis réinsère
  les passages et met à jour `documents` dans **une** transaction validée à la fin. Annulée en cours de route, le
  document garde son statut précédent (`pending` pour un nouveau), repris par le fil d'indexation (mécanisme de
  quarantaine du 22/11). Le `db.log(… "crash" …)` du gestionnaire écrit ensuite dans une transaction neuve. Cohérent.
- `mailin` (boucle par lettre) : une lettre en échec sur verrou perd ses écritures non validées et le gestionnaire
  l'enregistre en `erreur` (`_record`, qui réussit désormais) ; elle n'est pas retentée. Même issue qu'avant le
  correctif quand l'erreur n'était pas un verrou ; avant, `_record` échouait aussi et la connexion restait coincée.
  Pas d'état incohérent ; la reprise des lettres relève de #7.
- `watchdog.beat`, `monitor.report` : une écriture isolée suivie de `commit` ; rien à jeter.

Aucun cas incohérent trouvé.

**Hors périmètre** (deviennent des suites) :

- `monitor.report` perd l'alerte quand la base reste verrouillée plus de 30 s (repli sans base, par exemple envoi
  direct, à étudier).
- Faire alerter le chien de garde sur la taille du WAL ou sur l'âge de la dernière écriture de la base, symptôme
  qui a précédé les battements.
- Le gel de l'hôte (H4) : demander à OVH ou surveiller le vol de temps de calcul.
- Les « Rétabli » suivis d'une nouvelle alerte une minute plus tard (deux chiens de garde qui lisent des
  battements différents) : à comprendre.
- Les `exitCode=139` (erreurs de segmentation) du 25/11 sur un ancien conteneur : sujet distinct.

## Déploiement

Changement du code Python seulement ; ni schéma ni réglage. `scripts/ship.sh` reconstruit le site et le worker (ce
qui tue les `exec` en cours dans le worker). Premier démarrage : rien à rattraper, pas de fausse alerte attendue.
Retour arrière : `scripts/ship.sh --rollback`.

Vérifications après mise en production (lecture seule) :

```bash
ssh debian@152.228.141.68 'sudo docker logs --since 10m cop31-worker-1 2>&1 | grep -c "database is locked"'
```

```bash
ssh debian@152.228.141.68 'cd RAG_cop_climat && printf "%s\n" "import sqlite3" "c=sqlite3.connect(\"file:/app/data/cop.sqlite3?mode=ro\",uri=True)" "[print(*r) for r in c.execute(\"select key,value from meta where key like ?\",(\"hb:%\",))]" | sudo docker compose -f deploy/docker-compose.yml exec -T app python -'
```

```bash
ssh debian@152.228.141.68 'ls -la --time-style=full-iso RAG_cop_climat/data/cop.sqlite3*'
```

## Revue

Tour 1 (agent `reviewer`, 27/11) : **non bloquant**, critères 1 à 6 conformes, 207 tests verts.

- Accepté : tests manquants pour `commit` et `executescript`, exception d'origine non vérifiée, et test principal
  qui échouait sans correctif sur `in_transaction` plutôt que sur la réécriture. → `test_verrou_annule_et_remonte`
  (execute, executemany, executescript ; `sqlite_errorname` commence par `SQLITE_BUSY`), `test_commit_en_echec_sur_verrou`
  (refus simulé) ; sans correctif, le test principal échoue désormais sur la réécriture (« database is locked »).
- Gardé comme remarque : un verrou levé pendant un `fetchall` (pas par `execute`) ou dans un chemin C qui n'appelle
  pas la méthode Python n'est pas couvert ; aucun cas réel trouvé (en WAL, une lecture ne lève pas « locked »).

Pas de tour 2 nécessaire.

## Suites

À créer à la livraison (une issue `suite` par point hors périmètre ci-dessus).

## Promotion (à la livraison)

- [ ] PITFALLS § SQLite : « une écriture en échec sur verrou laisse la transaction ouverte ; la connexion reste
      coincée jusqu'au `rollback` » → `db.connect` s'en charge (testé)
- [ ] architecture.md : la connexion de `db.connect` annule sa transaction sur verrou
- [ ] suites → issues `suite`
- [ ] statut « livré » ici et dans l'index
