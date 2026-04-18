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
| Clés API (INSEE, Pappers) | `.env` local, jamais commité | 🔒 **Jamais en ligne** |

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
```

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

**Pipeline complet par défaut (Pappers désactivé) :**
```bash
python src/main.py
```

Enchaîne : extraction → DNS → scoring → génération de `public/data.json`. Zéro crédit consommé. Durée ≈ 15 s sur 300-500 entreprises.

**Options utiles** :
```bash
python src/main.py --dry-run             # simulation, aucun fichier écrit
python src/main.py --skip-extract        # réutilise data/raw_entreprises.json
python src/main.py --skip-extract --skip-dns
python src/main.py --naf 68.31Z,70.22Z   # override NAF (CSV)
python src/main.py --ville nice          # V1 : seule ville supportée
```

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
│   ├── check_dns.py        # étape 3 — DNS probing async
│   ├── score.py            # étape 4 — classification DNS + effectif
│   ├── enrich_pappers.py   # étape 5 — Pappers (désactivé par défaut)
│   └── main.py             # étape 6 — orchestrateur
├── public/                 # servi par GitHub Pages
│   ├── index.html          # [étape 7]
│   ├── style.css           # [étape 7]
│   ├── app.js              # [étape 7]
│   └── data.json           # généré par Python
└── data/
    ├── raw_entreprises.json
    ├── entreprises_avec_dns.json
    ├── shortlist.json
    └── cache/              # ignoré par git
```

### Barème de classification (V1)

Purement basé sur DNS + effectif, aucune dépendance payante :

| Signal DNS | Effectif | Classification |
|---|---|---|
| 0 domaine résolu | ≥ 10 salariés (tranches 11, 12, 21…) | **TRÈS PROBABLE** (rouge) |
| 0 domaine résolu | < 10 salariés | **PROBABLE** (orange) |
| Aucune variante générable (nom trop générique) | — | **À VÉRIFIER** (gris) |
| Au moins un domaine résolu | — | **ÉCARTÉ** (non affiché) |

### Cible de prospection

- Ville : Nice (CP 06000, 06100, 06200, 06300)
- Effectif : tranches INSEE 02, 03, 11, 12, 21
- Entreprises actives uniquement
- Codes NAF : 68.31Z, 70.22Z, 69.10Z, 41.20A

### Stockage perso

Chaque membre a son propre état local (contactés, notes, date de contact) stocké en `localStorage` dans son navigateur sous la clé `nosite_user_data_v1`. Aucune synchro entre les 3 en V1.

---

## Statut du projet

- [x] Étape 1 — Init (structure, requirements, workflow, check .env)
- [x] Étape 2 — Extraction API Recherche Entreprises (314 entreprises à Nice)
- [x] Étape 3 — DNS probing (151 prospects potentiels détectés)
- [x] Étape 4 — Scoring V1 (27 TRÈS PROBABLE, 96 PROBABLE, 28 À VÉRIFIER)
- [ ] Étape 5 — Pappers **différée** (trop coûteux en V1, voir section dédiée)
- [x] Étape 6 — Orchestrateur main.py (pipeline complet ≈ 15 s)
- [ ] Étape 7 — Frontend (carte Leaflet + sidebar + filtres + export CSV)
- [ ] Étape 8 — Déploiement GitHub Pages
