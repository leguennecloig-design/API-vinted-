# Vinted Niche Watch

Petite app locale qui surveille les **nouvelles annonces** d'une recherche Vinted, mesure combien de
favoris chacune prend dans ses *X* premières minutes, puis classe les annonces et surtout les **marques**.

## Installation et lancement (Windows)

1. Installe **Python 3.10 ou plus** depuis <https://www.python.org/downloads/> en cochant
   **« Add python.exe to PATH »**.
2. Double-clique sur **`start.bat`**.
   - Au premier lancement, il crée un environnement `.venv` et installe les dépendances (1–2 min).
   - Le serveur démarre et le navigateur s'ouvre sur <http://localhost:8000>.
3. Pour arrêter : ferme la fenêtre noire (ou `Ctrl+C`).

Les données sont stockées dans `data/niche_watch.db` (SQLite). Supprimer ce fichier = repartir de zéro.

### Depuis ton téléphone
La fenêtre de `start.bat` affiche une adresse du type `http://192.168.1.23:8000` : ouvre-la sur ton
téléphone connecté **au même Wi-Fi**. Si ça ne charge pas, autorise Python quand le pare-feu Windows
le demande (réseau **privé**), ou relance `start.bat` après avoir accepté.

## Utilisation

- **URL de recherche** (le plus simple) : fais ta recherche sur vinted.fr avec tous tes filtres (catégorie,
  prix, marques, tailles, état), puis copie l'URL de la barre d'adresse et colle-la dans l'app.
  Elle comprend `catalog[]`, `price_from`, `price_to`, `brand_ids[]`, `size_ids[]`, `status_ids[]`,
  `search_text` et le format `/catalog/79-sweats`.
- **Ou en manuel** : ID de catégorie + prix min/max (remplacent ceux de l'URL s'ils sont remplis).
- **Paramètres** : durée de veille (défaut 60 min), fenêtre de comptage (10 min), seuil de hit (20 favoris),
  intervalle entre scans (60 s, minimum 45 s).

Une seule veille à la fois. Après la durée de veille, l'app continue pendant la durée de la fenêtre pour
que les dernières annonces aient leur mesure complète. Les résultats restent dans l'**Historique** (export CSV
pour Excel, suppression possible).

### Trouver l'ID d'une catégorie
Sur vinted.fr, clique sur une catégorie (ex. Hommes › Sweats) : l'URL devient
`https://www.vinted.fr/catalog/79-sweats-and-hoodies` → l'ID est **79**.
Si tu filtres dans la recherche, l'URL contient `catalog[]=79` → même ID.

### Lire les résultats
- **Favoris fenêtre** = favoris au dernier relevé pris avant *fenêtre + 60 s* d'âge.
  « partielle » = ce relevé a été pris trop tôt (veille arrêtée, pause anti-blocage…).
- **Hit** = annonce qui atteint le seuil dans la fenêtre (ligne orange).
- **Score marque** = médiane des favoris × min(nb d'annonces, 5)/5 + 2 × hits.

## Mode démo
Coche **« Mode démo »** avant de lancer : flux d'annonces simulé, temps accéléré ×60 (1 min = 1 s),
aucun appel à Vinted. Pratique pour tester l'interface (une veille de 60 min dure ~70 s).

## Bon à savoir
- Les appels Vinted passent par le serveur Python (`curl_cffi`, empreinte Chrome). En cas de blocage
  (403/429), l'app ralentit d'elle-même (pause de 30 s, puis 1, 2, 4… min) et l'affiche.
- Aucune donnée sur les vendeurs n'est enregistrée : uniquement les annonces et leurs compteurs.
- Catégorie très active ? Au-delà de 15 pages par scan, un avertissement s'affiche : resserre les filtres.

## Structure
```
app/main.py      serveur FastAPI (API, exports CSV)
app/vinted.py    appels Vinted, lecture d'URL, flux démo
app/tracker.py   boucle de veille en tâche de fond
app/db.py        SQLite + calculs (marques, hits)
static/index.html  interface (une seule page)
start.bat        installation + lancement
```
