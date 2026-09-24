# ListingWatcher

Veille d'annonces (disques durs, mini PC, ou tout objet décrit par un profil) sur **leboncoin** et **eBay**
(FR + DE) avec notifications **ntfy** : nouvelle annonce
retenue, baisse de prix ≥ 5 %, digest quotidien, interface web de revue. Conteneur Docker, base SQLite,
secrets par variables d'environnement.

Plusieurs **veilles** cohabitent dans le même processus sans mélanger leurs flux : chacune a son profil,
ses paliers de prix, ses recherches, son onglet dans l'interface, son digest et son propre toggle de
notification. Les veilles livrées (`config.yaml > watches`) :

- `hdd` — disques durs 8 To, 3,5", SATA, CMR : catalogue de références, règles SMR/SAS/4Kn, prix au To.
  C'est la veille historique, celle qui notifie.
- `minipc` — mini PC 1 litre haut de gamme (Lenovo M90q / M70q / M80q / M75q Gen 2, OptiPlex 7080 / 7090 /
  7000 Micro, HP EliteDesk 800 G6 / 805 G6 / Elite Mini 800 G9…) avec un **plancher de performance** plutôt
  qu'un CPU exact (i5-10500T, 16 Go, 256 Go), plafond 600 € rendu. Veille **silencieuse** : stockée et revue
  dans l'interface, jamais notifiée (`notify.enabled: false`).

Le cœur est **générique** : sources, prix rendu, paliers, anti-arnaque, persistance, notifications et
interface ne connaissent pas l'objet surveillé. Ce qui est spécifique vit dans un **profil**
(`listingwatcher/profiles/`), choisi par `watches.<veille>.profile.type` :

- `hdd` — disques durs : catalogue, capacité cible, €/To.
- `pc` — PC : CPU ramené à un score (génération + gamme : i5-8400 = 10, comme un i7-7700 ou un i3-10100),
  RAM et stockage lus en Go, châssis par mots-clés, rejet des formats SFF/tour, des portables et des barebones
  signalés. Le libellé devient « Lenovo M720q i5-8500T », la médiane de marché se fait par CPU puis par châssis.
- `keywords` — générique, sans code : mots-clés requis / interdits, familles par mots-clés, référence
  par expression régulière, lots. Suffit pour une carte graphique, un vélo, un appareil photo…

Pour un objet qui demande plus d'intelligence, écrire une classe héritant de `Profile`
(`classify()`, éventuellement `unit_divisor`/`unit_label`, `attr_labels`, `low_flags`, `flag_notes`) et
l'ajouter au `REGISTRY`.

## Fonctionnement

À chaque créneau, les veilles sont scannées **l'une après l'autre**, source par source. Le transport est
partagé : un seul client leboncoin poli pour tout le processus (jamais deux requêtes en parallèle), un seul
client et jeton eBay, et une page déjà lue pendant le scan n'est pas relue par une autre veille.

1. **Fetch** — `listingwatcher/fetchers/leboncoin.py` lit les pages de résultats `/ck/<catégorie>/<slug>[/p-N]`
   (JSON `__NEXT_DATA__`, rendu côté serveur) avec un client à empreinte TLS de navigateur, une requête toutes
   les 6-11 s, robots.txt, backoff sur 403/429. `listingwatcher/fetchers/ebay.py` passe par la Browse API officielle.
2. **Normalisation (profil)** — le profil de la veille classe l'annonce : pour `hdd`, `listingwatcher/normalize.py`
   extrait la référence (ST8000VN004, WD80EFZZ, HUH728080ALE600, MG08ADA800E…) du titre + description, la classe
   via le catalogue puis des règles structurelles, rejette disques morts, SMR, SAS, 4Kn, externes, 2,5", NAS
   complets, capacités ≠ 8 To ; pour `pc`, `listingwatcher/profiles/pc.py` extrait CPU, RAM, stockage et châssis.
3. **Filtrage** — `listingwatcher/filtering.py` calcule le **prix rendu par article** (article + port vers la France
   + frais acheteur, divisé par la taille du lot), le ramène à l'unité du profil (€/To pour `hdd`), ignore
   les annonces plus vieilles que `notify.max_age_days`, le classe dans un palier (urgent / default / low / ignoré)
   et pose les drapeaux anti-arnaque (prix < 55 % de la médiane 30 j **de la veille**, vendeur sans évaluation
   sous le marché, port aberrant, ni livraison ni paiement sécurisé).
4. **Store** — `listingwatcher/store.py`, SQLite clé `(watch, source, listing_id)` : first/last_seen, statut
   (active / pending / sold / gone, le plus défavorable gagne), historique de prix, prix notifié. Une base
   antérieure aux veilles est migrée au démarrage, ses lignes rattachées à la veille `hdd`.
5. **Notification** — `listingwatcher/notify.py`, un notifieur par veille : une alerte par nouvelle annonce retenue ou
   baisse ≥ 5 %, jamais de doublon ; digest à 8 h des annonces encore actives. Une veille silencieuse
   n'envoie rien. Les alertes techniques (source bloquée) partent toujours, sur le topic global.
6. **Interface web** — `listingwatcher/web.py`, port 8080 du conteneur (8004 sur l'hôte) : un onglet par veille,
   revue des annonces (filtres statut / retenues / source / revue, recherche, tri), favoris ★, vues ✓,
   ignorées ✕ (plus de notification ni de digest), notes, bouton « Scanner maintenant » (toutes les veilles),
   et un onglet **⚙ Réglages** qui édite la config (voir plus bas).
   API JSON sous `/api/` : `watches`, `stats?watch=`, `listings?watch=`, `listings/<source>/<id>/history?watch=`,
   `listings/<source>/<id>/review?watch=`, `scan`, `health`, `settings` (+ `settings/…` en POST). Sans `watch`,
   la première veille.

Les annonces retenues sont **enrichies en second temps** (fiche `/ad/` leboncoin pour la description et les
heures SMART, `getItem` eBay pour quantité, retours, MPN), jamais les autres.

## Installation

```bash
git clone <ce dépôt> && cd ListingWatcher
cp .env.example .env        # remplir NTFY_* et EBAY_*
docker compose build
docker compose run --rm listingwatcher test-notify      # vérifie ntfy
docker compose run --rm listingwatcher probe --watch minipc --source leboncoin   # fetch + classification, rien n'est enregistré
docker compose up -d        # service continu
docker compose logs -f
```

La base vit dans `./data/listingwatcher.sqlite` (volume `/data`), la config éditable dans `./data/config.yaml`
(voir « Réglages depuis l'interface »).

### Identifiants eBay

Sur <https://developer.ebay.com> : *My Account → Application Keys → Production* → créer un keyset.
Copier **App ID (Client ID)** et **Cert ID (Client Secret)** dans `.env`. La Browse API en
*client credentials* suffit (scope `https://api.ebay.com/oauth/api_scope`), aucun consentement utilisateur.
Sans ces variables, la source eBay est désactivée proprement au démarrage.

### Sur un hôte Docker distant

Construire l'image sur le poste (`docker build -t listingwatcher:latest .`), la transférer
(`docker save listingwatcher:latest | gzip | ssh <hôte> 'gunzip | docker load'`) et la lancer avec le
`docker-compose.yml` du dépôt ou l'outil de l'hôte (Portainer, Container Manager…). Les secrets restent des
variables d'environnement du service, jamais dans l'image ; le volume `/data` porte la base et la config
éditable. Interface : `http://<hôte>:8004/`, sans authentification, à garder sur le réseau local.

### Sur un Synology (DSM)

Deux options :

- **Container Manager** : importer le projet (`docker-compose.yml`), le service `run` tourne en continu.
- **Planificateur de tâches** : lancer `docker compose run --rm listingwatcher scan` 2 à 4 fois par jour et
  `… digest` à 8 h. Le mode `scan` fait un passage puis s'arrête, et la base porte la mémoire des annonces.

## Commandes

| Commande | Rôle |
|---|---|
| `run` | service : interface web + scans de toutes les veilles aux créneaux de `schedule.scan_times` + digests à `digest_time` |
| `serve` | interface web seule (quand les scans sont lancés par un cron / le planificateur DSM) |
| `scan [--watch X] [--source leboncoin\|ebay]` | un passage complet, puis sortie |
| `digest [--watch X] [--force]` | envoie le digest des annonces actives (veilles qui notifient) |
| `probe [--watch X] [--source X] [--all]` | fetch + classification, affiché à l'écran, rien enregistré |
| `classify "<titre>" [--watch X] [--desc …] [--price N]` | teste le classifieur d'une veille sur un titre |
| `test-notify` | envoie une notification de test sur le topic global |
| `stats` | état de la base, total et par veille |

Options globales : `--dry-run` (rien n'est envoyé à ntfy), `--config` (un fichier pris tel quel, sans copie
éditable), `--db`, `--log-level`.
`--watch` prend une clé de `watches` ; sans lui, toutes les veilles (ou la première pour `classify`).

## Configuration (`config.yaml`)

- Racine : `timezone`, `schedule`, `web`, et les **valeurs par défaut** de `notify`, `market`, `scam`,
  `thresholds` que chaque veille peut surcharger.
- `sources.<source>` : le **transport** commun — `min_delay_s`/`max_delay_s`, `respect_robots`,
  `cooldown_hours_when_blocked`, `detail_pages`, `shipping_estimate` (leboncoin n'expose pas le « dès X € »,
  on l'estime au poids déclaré) ; `marketplaces`, `condition_ids`, `delivery_zip`, `limit` (eBay).
- `watches.<veille>` : `title`, `enabled`, `notify` (`enabled`, `price_drop_pct`, `suspicious`
  (`separate` = alerte à part en priorité basse, `never`), `require_delivery`, `max_age_days`, `topic` ntfy
  propre, `label` préfixe des titres), `thresholds` (paliers sur le prix rendu par article → priorité ntfy et
  tags), `market.reference_unit_price`, `scam`, `profile` (`type` + section du même nom) et `sources` :
  `leboncoin.searches` (couples catégorie/slug), `ebay.queries`, `category_ids`, `price_min`/`price_max`,
  `condition_ids`. Une veille qui ne déclare pas une source ne la scanne pas.
- `profile.hdd.models` : catalogue accepté / surveillance / rejeté ; `profile.hdd.keywords` : mots-clés
  (mort, SMR, SAS, externe…). `profile.pc` : `min_cpu`, `min_ram_gb`, `min_storage_gb`, `families`,
  `require_family`, `reject`, `reject_title`, `reject_form`, `mini_any` (détail dans `listingwatcher/profiles/pc.py`).

Un `config.yaml` à l'ancien format (profil et recherches à la racine, sans `watches`) reste accepté : il est
enveloppé dans une veille unique `hdd`. Les variables `${VAR}` / `${VAR:-défaut}` sont substituées depuis
l'environnement.

## Réglages depuis l'interface

Le `config.yaml` de l'image n'est qu'une **graine**. Au premier démarrage du service (`run` ou `serve`), il est
copié dans le volume de données (`/data/config.yaml`, ou `$LISTINGWATCHER_LIVE_CONFIG`, par défaut à côté de la base),
et c'est **cette copie qui fait foi** ensuite, pour toutes les commandes. `--config <fichier>` court-circuite la
copie (lecture seule, onglet Réglages désactivé).

L'onglet **⚙ Réglages** édite cette copie :

- **Réglages généraux** : créneaux de scan, aléa, digest, titre, valeurs par défaut de `notify`, `market`,
  `scam` et des paliers. Le transport des sources (délais, robots, marketplaces, estimation du port) reste dans
  le fichier.
- **Une fiche par veille**, y compris désactivée : titre, activation, notification (vide = hérité de la racine,
  valeur affichée), paliers, prix de référence, anti-arnaque, recherches leboncoin (avec lien ↗ vers la page),
  requêtes eBay, et le **profil** dans un éditeur YAML (catalogue, familles, mots-clés). Un bouton bascule la veille
  entière en YAML pour les clés que le formulaire ne connaît pas.
- **Créer, dupliquer** (la copie naît désactivée), **activer / désactiver**, **supprimer** une veille. La clé d'une
  veille est définitive : elle rattache les annonces en base, qui survivent à la suppression.
- **Tester le classifieur** : un titre, une description, un prix → verdict, drapeaux, palier et signaux
  anti-arnaque, calculés avec les réglages affichés, même non enregistrés.

Chaque enregistrement est **validé** (profil chargé, expressions régulières, paliers, recherches, créneaux,
veilles désactivées comprises) avant d'être écrit, puis le service se **reconstruit à chaud** : tout de suite, ou à
la fin du scan en cours. L'édition passe par ruamel.yaml : commentaires, ordre des clés et `${VAR}` sont conservés
(seules les longues listes en flux repliées sur plusieurs lignes sont remises sur une ligne). Les 10 versions
précédentes sont gardées dans `config-history/` à côté de la copie, consultables et restaurables depuis l'onglet.

Quand le `config.yaml` de l'image change (nouveau déploiement), ses nouveautés **ne s'appliquent pas** d'elles-mêmes :
un bandeau le signale dans les réglages généraux, avec un bouton « Réinitialiser depuis l'image » (la version
courante part dans l'historique). L'interface n'a pas d'authentification : elle reste sur le LAN.

## Mode dégradé

Si leboncoin renvoie un blocage persistant (403/429), la source est mise en pause pour toutes les veilles
(`cooldown_hours_when_blocked`) et une notification unique par jour le signale. Repli : la fonction native
« Sauvegarder la recherche » de leboncoin envoie des alertes par mail.

Note : l'en-tête de `robots.txt` de leboncoin déclare interdire les accès automatisés, mais ses règles ne
bloquent pas `/ck/` ni `/ad/accessoires_informatique/`. Le client reste volontairement lent et séquentiel.

## Ajouter une veille

Depuis l'interface : ⚙ Réglages → « + Nouvelle veille » (ou « Dupliquer » une veille proche), tester quelques
titres réels avec le classifieur, enregistrer. Rien à redéployer.

À la main :

1. Une entrée sous `watches` : `title`, `notify.enabled`, `thresholds`, `market.reference_unit_price`,
   `sources` (recherches), `profile` (`type` existant, ou nouveau profil ci-dessous).
2. `python -m listingwatcher probe --watch <nom> --source leboncoin --all` pour valider slugs et classification.
3. Éditer `data/config.yaml` (la copie qui fait foi) et relancer, ou modifier le `config.yaml` du dépôt,
   redéployer et « Réinitialiser depuis l'image ».

## Ajouter une source

1. Créer `listingwatcher/fetchers/<source>.py` avec une classe héritant de `BaseFetcher` (`name` court,
   `fetch() -> FetchResult`, optionnellement `enrich(listing)`, `supports_enrich = True` et
   `make_transport(scfg)` pour le client partagé entre veilles).
   Produire des `Listing` (`listingwatcher/models.py`) : prix, port vers la France (None si inconnu), frais,
   état, vendeur, statut.
2. L'enregistrer dans `REGISTRY` (`listingwatcher/fetchers/__init__.py`), clé = section `sources.<source>` de
   `config.yaml` ; les clés de recherche par veille sont listées dans `config.SEARCH_KEYS`.
3. Ajouter la section de config et un test avec une fixture.

## Tests

```bash
python -m pytest -q
```

Le classifieur disques est couvert par les titres réels du brief et par ceux relevés en scannant leboncoin
(fixtures `tests/fixtures/lbc_page*.json`, capturées le 9 septembre 2026). `tests/test_multiwatch.py` couvre
l'isolation des veilles, la migration de base, le transport partagé, la veille silencieuse, les onglets et le
profil `pc`.
