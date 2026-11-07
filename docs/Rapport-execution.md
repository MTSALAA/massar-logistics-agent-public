# Rapport d'exécution – Agent Logistique / Livraison (Équipe Massar)

Projet *Conduite de projet*, ESTIN, 2026-2027. Version de l'agent : **1.2.1**. Filière : `automatisation_metier`. Slug : `massar-logistique`.

Ce rapport décrit comment lancer l'agent, ce qu'il fait, quels appels réseau il émet, quelles données il conserve, et comment chaque point de la liste de vérification du guide du développeur a été contrôlé.

## 1. Rôle de l'agent

Un service autonome (API REST dans une image Docker) qui transforme chaque commande dont le stock est disponible en expédition planifiée sur une tournée optimisée, suit l'expédition jusqu'à la livraison et publie l'objet `expedition` au noyau.

| | Objets |
|---|---|
| Consomme (`consumes`) | `commande`, `stock_level` |
| Produit (`produces`) | `expedition` |

## 2. Lancement

```bash
docker compose up --build          # l'agent écoute sur le port 9000
curl http://localhost:9000/health
```

L'image démarre sans autre configuration que des variables d'environnement, toutes facultatives :

| Variable | Rôle | Valeur par défaut |
|----------|------|-------------------|
| `DATABASE_PATH` | Fichier SQLite (dans un volume Docker) | `/data/agent.db` |
| `CORE_URL` | Adresse du noyau vue depuis le conteneur (publication, lecture, alertes) | `http://host.docker.internal:8000` dans `docker-compose.yml` ; sans noyau joignable l'agent continue et réessaie plus tard |
| `NOMINATIM_URL` | Géocodage des adresses ; vide = désactivé | Service public Nominatim dans `docker-compose.yml`, désactivé si la variable n'est pas définie |
| `NOMINATIM_CONTACT`, `NOMINATIM_COUNTRY` | Identification demandée par la politique d'usage ; pays (`dz`) | vides / `dz` |
| `OSRM_URL` | Réseau routier ; vide = distances à vol d'oiseau | vide |
| `SESSION_SECRET`, `SESSION_COOKIE_SECURE` | Signature du cookie de l'interface web ; cookie `Secure` derrière un proxy HTTPS | aléatoire au démarrage / non |

Aucun secret n'est écrit dans le code (contrôlé par un test automatique).

## 3. Liste de vérification du guide du développeur

| Point | Comment il a été vérifié | Résultat |
|-------|--------------------------|----------|
| Les 5 endpoints répondent avec l'enveloppe `{status, data, error}` | Test automatique qui appelle **chaque route** de l'agent sans en-têtes, avec une mauvaise clé et avec un tenant inconnu, et exige l'enveloppe ; mêmes exigences pour routes inconnues, mauvaises méthodes, corps mal formés et erreur interne (500) ; contrôle manuel avec `curl` sur une image neuve | Conforme |
| Manifeste valide, slug unique, filière renseignée | Tests automatiques (champs, filière, version, cohérence avec le code et avec le Dockerfile) ; soumission au noyau local acceptée | Conforme ; l'unicité du slug se confirme au moment de la soumission réelle |
| Isolation par `tenant_id` | Tests : deux entreprises avec un scénario complet (configuration, stock, commandes, plan, livraison, échec, codes d'accès) ; aucune route ni page ne montre une donnée de l'autre ; la clé d'une entreprise n'ouvre rien chez l'autre ; contrôle direct du fichier SQLite ; contrôle manuel sur image neuve | Conforme, 0 fuite |
| Le Dockerfile fonctionne | `docker build` puis `docker run` sans volume ni réglage manuel ; la sonde de santé du conteneur passe à `healthy` | Conforme |
| `POST /tenant/deactivate` supprime réellement les données | Test qui lit les tables **dans le fichier SQLite** (sans se fier à la liste du code), vérifie 0 ligne pour l'entreprise désactivée dans chacune, et le même nombre de lignes qu'avant pour l'autre ; contrôle manuel dans le conteneur ; test sur le noyau local (annulation d'une souscription) | Conforme |
| `/exchange/notify` testé avec un objet factice | Tests automatiques (objet accepté, type inconnu ignoré, champs inconnus ignorés, version ancienne ignorée, mauvaise clé refusée) ; contrôle manuel | Conforme |

Vérification supplémentaire avec le noyau de l'enseignant, lancé en local : soumission, certification, publication, souscription (activation), publication d'objets `expedition` (versions successives), envoi d'alertes au webhook, annulation de la souscription (purge). Les commandes et niveaux de stock ont été envoyés directement à l'agent ; leur publication **par le noyau lui-même** n'a pas encore été testée (elle demande une session d'entreprise sur le noyau).

## 4. Appels réseau sortants

| Destination | Quand | Ce qui part |
|-------------|-------|-------------|
| Noyau (`CORE_URL`) : `POST /exchange/agent/expedition` | À chaque création ou changement d'état d'une expédition | L'expédition (numéro, référence de commande, transporteur, numéro de suivi, statut) ; clé et identifiant de l'entreprise en en-têtes |
| Noyau : `GET /exchange/agent/commande` et `stock_level` | Au démarrage, à l'activation, à la demande | Clé et identifiant de l'entreprise |
| Noyau : `POST /webhooks/agent-events` | Une fois par alerte ouverte | Type, gravité, référence de commande, message |
| Nominatim (`NOMINATIM_URL`, service public d'OpenStreetMap) | Adresse en texte sans coordonnées en cache | Le texte de l'adresse et le code pays ; au plus 1 requête par seconde, en-tête `User-Agent` identifiant l'application, réponses mises en cache ; une même adresse n'est demandée qu'une seule fois, même si plusieurs commandes arrivent ensemble (vérifié par un test) |
| OSRM (`OSRM_URL`, conteneur de l'équipe) | À chaque plan, si OSRM est configuré (matrice des distances, puis tracé routier de chaque véhicule) | Des coordonnées, vers la machine de l'équipe |
| Google Maps / Apple Maps (liens ouverts par le **téléphone du livreur**, pas par l'agent) | Quand le livreur touche « Navigate » | Les coordonnées de l'arrêt (ou des dix prochains arrêts), envoyées par le navigateur du livreur |
| Navigateur de l'utilisateur de l'interface web (pas l'agent) | Affichage des pages | Chargement de HTMX et de Leaflet depuis `unpkg.com` et des tuiles de carte depuis `tile.openstreetmap.org` |
| Pendant la construction de l'image uniquement | `docker build` | Téléchargement des paquets Python (PyPI) et de l'image de base |

L'agent n'appelle jamais directement un autre agent. Aucun autre service n'est contacté.

## 5. Données conservées

Un fichier SQLite par instance. **Toutes** les tables portent `tenant_id`, et toutes sont listées dans `TENANT_TABLES`, qui pilote la suppression :

| Table | Contenu |
|-------|---------|
| `tenants` | Entreprise, clé d'accès reçue du noyau (nécessaire pour appeler le noyau ; jamais écrite dans les journaux), configuration validée |
| `objects` | Commandes et niveaux de stock reçus |
| `order_decisions`, `alerts` | Décision prise pour chaque commande ; alertes ouvertes et résolues |
| `shipments`, `shipment_events` | Expéditions et historique horodaté (UTC) |
| `geocache` | Adresses déjà géocodées (propre à chaque entreprise) |
| `route_plans`, `route_stops` | Plans de tournées et arrêts |
| `ui_access` | Empreintes salées (PBKDF2) des codes d'accès de l'interface ; jamais les codes eux-mêmes |

Un test vérifie que toute table contenant `tenant_id` figure dans la liste de suppression : une table ajoutée plus tard et oubliée fera échouer les tests.

## 6. Sécurité

- Chaque appel est identifié par `X-Tenant-Id` ; la clé reçue à l'activation est comparée (comparaison à temps constant) pour `/exchange/notify`, `/tenant/deactivate` et les routes d'exploitation. Une clé fausse ou celle d'une autre entreprise donne 401.
- L'activation elle-même ne peut pas être authentifiée (c'est elle qui apprend la clé) ; le contrat ne prévoit qu'un en-tête fixe `X-Platform-Signature` côté noyau.
- Aucune donnée sensible dans les journaux : seuls les types d'erreur sont journalisés (contrôlé par un test pour la clé).
- Interface web (`/ui`) : codes d'accès par entreprise (gestionnaire, livreur) stockés hachés, cookie de session signé (`HttpOnly`, `SameSite=Lax`), blocage après 5 essais ratés par minute, refus des actions qui ne viennent pas de l'interface, échappement de tout texte issu des commandes (testé sur toutes les pages).
- Les pages `/ui` répondent en HTML, et la documentation automatique de FastAPI (`/docs`) en HTML/JSON : ce ne sont pas des routes du contrat et le noyau ne les appelle jamais. Toutes les routes que le noyau ou un opérateur appelle respectent l'enveloppe.

### Audit de sécurité (version 1.2.1)

Les fichiers et tout l'historique git ont été analysés (aucun secret trouvé), le code avec `bandit` (aucun problème), les dépendances avec `pip-audit` (24 vulnérabilités connues corrigées par mise à jour ; aucune restante). Corrections : limite de taille des requêtes (1 Mo), en-têtes contre l'inclusion des pages dans un cadre et la mise en cache, valeurs d'attributs encodées, scripts externes verrouillés par empreinte cryptographique, mémoire bornée pour les échecs de connexion, limite de 500 expéditions par plan, conteneur exécuté sans privilèges administrateur. Détail : `docs/how-it-works/iteration-8-security-review.md` et `SECURITY.md`.

## 7. Performances mesurées

Poste de développement (Windows, Python 3.12) ; jeu d'essai synthétique et répétable (`scripts/benchmark_routing.py`) : 50 arrêts tirés au hasard (graines 1 à 5) dans une zone d'environ 20 km autour de Béjaïa, 4 véhicules de capacité 40, distances à vol d'oiseau.

| Mesure | Résultat | Objectif de la charte |
|--------|----------|-----------------------|
| Plan de 50 arrêts | 5,0 s (durée maximale donnée au solveur) | moins de 10 s |
| Distance économisée par rapport à la séquence de saisie des commandes | de 74,5 % à 76,3 %, moyenne 75,4 % | au moins 15 % |
| Arrêts servis | 50 sur 50 | – |
| Prise en compte d'une commande (réception, décision, enregistrement) | de l'ordre de 60 à 80 ms | le noyau laisse 10 s à un agent |
| Fuites de données entre entreprises (tests automatiques) | 0 | 0 |

**À lire avec prudence :** la séquence de référence est l'ordre de saisie de commandes dont les adresses sont tirées au hasard, soit le **pire cas** pour la référence ; les vraies commandes sont en partie regroupées par quartier, donc le gain réel sera plus faible. Les distances à vol d'oiseau sous-estiment les distances routières. Le taux de livraisons à l'heure n'a pas encore été mesuré sur un jeu de test réel.

## 8. Tests automatiques

Plus de 270 tests (`cd agent && pytest`) et le contrôle de style `ruff`, tous deux inscrits dans la configuration d'intégration continue (GitHub Actions, dont l'exécution reste à observer) : conformité au contrat, isolation, suppression, configuration, décision d'expédition, cycle de vie, publication au noyau (noyau simulé), géocodage et routage (services simulés), solveur et jeu d'essai de 50 arrêts, interface web, indicateurs, alertes, migration d'anciennes bases, concurrence (8 notifications simultanées de la même commande = 1 expédition).

## 9. Limites connues et points non vérifiés

- Nominatim a été essayé en réel (15 adresses de lieux publics de Béjaïa : 13 trouvées, 2 inconnues, sans requête en double). OSRM a été essayé en réel sur la carte d'Algérie (301 Mo, préparée en environ 9 minutes) : distances et durées routières, tracé des routes sur la carte, détection d'un point en mer (3 408 m de la route la plus proche) et plan de 9 arrêts en 4,2 s avec 37,2 % de distance économisée par rapport à la séquence de saisie.
- La publication d'objets par le noyau lui-même (et non envoyés directement à l'agent) reste à tester avec une session d'entreprise.
- L'interface web a été contrôlée par des tests automatiques et à la main dans un seul navigateur, en affichage ordinateur et en affichage téléphone (émulé) ; pas sur un vrai téléphone. Elle charge HTMX et Leaflet depuis un CDN public.
- Pas de réservation de stock : l'agent Stock en reste propriétaire. Deux commandes visant les dernières unités peuvent partir toutes les deux (question ouverte dans `docs/Adresse-livraison-proposition.md`).
- Les alertes de retard et les renvois vers le noyau sont traités toutes les 5 minutes par une tâche de fond (variable `MAINTENANCE_SECONDS`).
- Sans OSRM, les distances sont des estimations (ligne droite × 1,3, alors que le rapport réel varie : 2,2 pour un trajet mesuré à Béjaïa) et l'agent ne peut pas repérer un point situé en mer ; avec OSRM, un point à plus de 500 m de toute route déclenche une alerte.
- La navigation pas à pas est assurée par l'application de cartes du téléphone du livreur (Google Maps ou Apple Maps) ; l'agent ne suit pas la position du livreur.
- Le gain mesuré porte sur un jeu synthétique ; voir la mise en garde du paragraphe 7.
