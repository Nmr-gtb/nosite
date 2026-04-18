# Nosite

Outil interne de prospection B2B — détecte les entreprises de Nice qui n'ont pas de site internet, les affiche sur une carte, et permet de les exporter.

Équipe : 3 freelances. Stack : Python pour l'extraction, HTML/CSS/JS vanilla pour le frontend. Hébergement : GitHub Pages (statique) pour le code, **Gist GitHub privé** pour les données.

## Architecture data / code

Pour pouvoir publier le code sur GitHub sans exposer la liste de nos leads :

| Quoi | Où | Public ? |
|---|---|---|
| Code source (Python + HTML/CSS/JS) | Repo GitHub `nosite` | 🌐 **Public** |
| URL du Gist privé | `public/config.js` (placeholder en public, vraie URL en local) | — |
| Liste des 151 entreprises (`data.json`) | **Gist GitHub privé** | 🔒 **Privé** |
| Clés API (INSEE, Pappers, Serper) | `.env` local, jamais commité | 🔒 **Jamais en ligne** |

Le frontend GitHub Pages charge `public/config.js` (commité avec un placeholder), chaque membre remplace localement l'URL par celle du Gist, et l'UI fetch les données depuis ce Gist au démarrage.

**Conséquence** : aucun SIREN, nom d'entreprise ou adresse de prospect n'apparaît publiquement sur GitHub.

---

## Pour les 3 membres de l'équipe

### Premier setup (une seule fois)

```bash
# 1. Cloner le repo
git clone <url-du-repo>
cd nosite

# 2. Créer l'environnement Python
python3 -m venv .venv
source .venv/bin/activate

# 3. Installer les dépendances
pip install -r requirements.txt

# 4. Créer ton fichier .env à la racine
# (Noé te transmet les clés en privé — ne JAMAIS les commit)
cp .env.example .env  # puis édite avec tes clés
```

Le fichier `.env` doit contenir :

```
INSEE_API_KEY=...
PAPPERS_API_KEY=...
SERPER_API_KEY=...
```

> `SERPER_API_KEY` est nécessaire en V2 pour la détection de site web via Google. Compte gratuit : 2500 crédits sur https://serper.dev/.

### 5. Configurer l'URL du Gist privé (local uniquement)

Noé te partagera **l'URL raw du Gist privé** qui contient les données des 151 entreprises. Ouvre `public/config.js` et remplace le placeholder :

```js
window.NOSITE_CONFIG = {
  dataUrl: "https://gist.githubusercontent.com/ton-pseudo/.../raw/data.json",
};
```

> ⚠️ Ne commit pas cette modification. Elle reste locale. Le repo public garde toujours le placeholder.

### Vérifier que tout est prêt

```bash
python src/check_env.py
```

Tu dois voir `✅ Clés chargées`.

### Actualiser les données (quand tu veux rafraîchir la carte)

**Pipeline complet V2 (Serper activé, Pappers désactivé) :**
```bash
python src/main.py
```

Enchaîne : extraction → **détection site web via Serper** → scoring → génération de `public/data.json`. ~1 crédit Serper par entreprise (quota gratuit 2500). Durée ≈ 1 min sur 200 entreprises.

**Options utiles** :
```bash
python src/main.py --dry-run                   # simulation, aucun fichier écrit
python src/main.py --skip-extract              # réutilise data/raw_entreprises.json
python src/main.py --skip-extract --skip-site  # réutilise aussi la détection Serper
python src/main.py --limit-serper 20           # plafonne à 20 appels Serper
python src/main.py --legacy-dns                # fallback V1 (DNS uniquement)
python src/main.py --naf 68.31Z,70.22Z         # override NAF (CSV)
python src/main.py --naf 69.10Z                # réactive explicitement les juristes
python src/main.py --ville nice                # V1 : seule ville supportée
```

> Par défaut, le NAF 69.10Z (Activités juridiques) est exclu : les notaires et avocats ont tous un site via leurs ordres professionnels.

**Push des données fraîches sur GitHub :**
```bash
git add public/data.json
git commit -m "refresh data"
git push
```

GitHub Pages redéploie automatiquement. L'URL reste la même pour les 3 membres.

### URL de l'outil

*À compléter après activation de GitHub Pages* — par exemple : `https://<pseudo-github>.github.io/nosite/`.

---

## Pappers — DÉSACTIVÉ par défaut (V1)

### Pourquoi ?

Mesuré empiriquement (avril 2026) sur notre compte gratuit (100 jetons) :

| Type d'appel | Coût réel |
|---|---|
| `/v2/entreprise` sans champs supplémentaires | **1 jeton** |
| `/v2/entreprise?champs_supplementaires=telephone,email` | **7 jetons** |
| Paramètre `site_web` dans `champs_supplementaires` | **non reconnu** — aucun champ site web n'est exposé dans la réponse |

**Conséquence** : enrichir les 80 meilleurs prospects coûte **560 jetons**. Impossible sur le quota gratuit. On garde les 67 crédits restants en réserve pour **enrichir manuellement** les leads que l'équipe décide de vraiment attaquer.

### Réactiver ponctuellement

```bash
# Enrichir les 5 meilleurs prospects (coût ≈ 35 jetons)
python src/main.py --enable-pappers --limit-pappers 5 --yes
```

Le script affiche une confirmation AVANT consommation et interroge `/v2/suivi-jetons` (gratuit) pour afficher le solde réel après. Cache 30 jours sur les appels — un SIREN déjà vu ne recoûte jamais.

### Diagnostic du solde à tout moment

```bash
curl -s "https://api.pappers.fr/v2/suivi-jetons?api_token=$(grep PAPPERS .env | cut -d= -f2)" | python3 -m json.tool
```

---

## Architecture technique

```
nosite/
├── .env                    # clés API (NE PAS COMMIT)
├── .env.example
├── .gitignore
├── .github/workflows/
│   └── pages.yml           # déploiement auto
├── requirements.txt
├── src/
│   ├── __init__.py
│   ├── check_env.py        # vérifie .env
│   ├── extract.py          # étape 2 — API Recherche Entreprises
│   ├── detect_website.py   # étape 3 V2 — recherche Google via Serper
│   ├── check_dns.py        # étape 3 V1 legacy — DNS probing async
│   ├── score.py            # étape 4 — classification (Serper prioritaire)
│   ├── enrich_pappers.py   # étape 5 — Pappers (désactivé par défaut)
│   └── main.py             # étape 6 — orchestrateur
├── public/                 # servi par GitHub Pages
│   ├── index.html
│   ├── style.css
│   ├── app.js
│   └── data.json           # généré par Python
└── data/
    ├── raw_entreprises.json
    ├── entreprises_avec_site.json   # V2 (Serper)
    ├── entreprises_avec_dns.json    # V1 legacy (DNS)
    ├── shortlist.json
    └── cache/              # ignoré par git
```

### Barème de classification (V2 Serper)

Priorité donnée à la recherche Google réelle via Serper. DNS reste en fallback si `--legacy-dns`.

| Signal Serper | Effectif | Classification |
|---|---|---|
| Site détecté dans le top 10 Google | — | **ÉCARTÉ** (non affiché) |
| Aucun site trouvé | ≥ 10 salariés | **TRÈS PROBABLE** (vert) |
| Aucun site trouvé | < 10 salariés | **PROBABLE** (jaune) |
| Entreprise non traitée (quota/limit) | — | **À VÉRIFIER** (gris) |

### Cible de prospection

- Ville : Nice (CP 06000, 06100, 06200, 06300)
- Effectif : tranches INSEE 02, 03, 11, 12, 21
- Entreprises actives uniquement
- Codes NAF **par défaut** : 68.31Z, 70.22Z, 41.20A (69.10Z exclu — couvert par les ordres professionnels)

### Stockage perso

Chaque membre a son propre état local (contactés, notes, date de contact) stocké en `localStorage` dans son navigateur sous la clé `nosite_user_data_v1`. Aucune synchro entre les 3 en V1.

---

## Statut du projet

- [x] Étape 1 — Init (structure, requirements, workflow, check .env)
- [x] Étape 2 — Extraction API Recherche Entreprises
- [x] Étape 3 V1 — DNS probing (legacy, disponible via `--legacy-dns`)
- [x] Étape 3 V2 — Détection de site web via Serper (Google) ✨
- [x] Étape 4 — Scoring (V2 priorise Serper, fallback DNS)
- [ ] Étape 5 — Pappers **différée** (trop coûteux, voir section dédiée)
- [x] Étape 6 — Orchestrateur main.py (V2 par défaut)
- [x] Étape 7 — Frontend (carte Leaflet + sidebar + filtres + export CSV)
- [x] Étape 8 — Déploiement GitHub Pages
