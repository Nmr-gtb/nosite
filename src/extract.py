"""
Étape 2 — Extraction des entreprises depuis l'API Recherche Entreprises.

API utilisée : https://recherche-entreprises.api.gouv.fr/search
- Gratuite, aucune clé requise
- Max 25 résultats par page
- On itère sur chaque code NAF pour contourner la limite

Cible :
- Ville : Nice (CP 06000, 06100, 06200, 06300)
- Effectif : tranches INSEE 02, 03, 11, 12, 21
- État : actif uniquement
- NAF : 68.31Z (immo), 70.22Z (conseil), 69.10Z (juridique), 41.20A (constr.)
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --- Constantes ---

API_URL = "https://recherche-entreprises.api.gouv.fr/search"

CODES_POSTAUX = ["06000", "06100", "06200", "06300"]

CODES_NAF = [
    ("68.31Z", "Agences immobilières"),
    ("70.22Z", "Conseil pour les affaires et autres conseils de gestion"),
    ("69.10Z", "Activités juridiques"),
    ("41.20A", "Construction de maisons individuelles"),
]

TRANCHES_EFFECTIF = ["02", "03", "11", "12", "21"]

PER_PAGE = 25
MAX_PAGE = 200
DELAI_ENTRE_REQUETES = 0.3  # secondes, bon citoyen vis-à-vis de l'API

ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = ROOT / "data" / "raw_entreprises.json"


# --- Session HTTP avec retry automatique ---

def creer_session() -> requests.Session:
    """Session HTTP avec retry exponentiel sur erreurs réseau et 5xx."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "nosite/1.0 (prospection interne)"})
    return session


# --- Extraction ---

def extraire_page(
    session: requests.Session,
    code_naf: str,
    page: int,
    codes_postaux: list[str],
    tranches_effectif: list[str],
) -> dict:
    """Appelle l'API pour un code NAF et une page donnés."""
    params = {
        "activite_principale": code_naf,
        "code_postal": ",".join(codes_postaux),
        "tranche_effectif_salarie": ",".join(tranches_effectif),
        "etat_administratif": "A",
        "per_page": PER_PAGE,
        "page": page,
    }
    reponse = session.get(API_URL, params=params, timeout=15)
    reponse.raise_for_status()
    return reponse.json()


def siege_dans_perimetre(entreprise: dict, codes_postaux: list[str]) -> bool:
    """
    L'API filtre sur n'importe quel établissement matchant le CP : on re-filtre
    côté client pour ne garder que les entreprises dont le SIÈGE est dans
    le périmètre et administrativement actif.
    """
    siege = entreprise.get("siege") or {}
    return (
        siege.get("code_postal") in codes_postaux
        and siege.get("etat_administratif") == "A"
    )


def extraire_toutes(
    codes_postaux: list[str] = CODES_POSTAUX,
    codes_naf: list[tuple[str, str]] = CODES_NAF,
    tranches_effectif: list[str] = TRANCHES_EFFECTIF,
) -> tuple[list[dict], int]:
    """
    Itère sur chaque NAF et chaque page, déduplique par SIREN, puis filtre
    pour ne garder que les sièges dans le périmètre.
    Retourne (entreprises_filtrees, total_brut).
    """
    session = creer_session()
    entreprises: dict[str, dict] = {}  # indexé par SIREN pour dédupliquer

    for code_naf, libelle_naf in codes_naf:
        print(f"\n🔎 NAF {code_naf} — {libelle_naf}")
        page = 1
        total_naf = 0

        while page <= MAX_PAGE:
            try:
                data = extraire_page(
                    session, code_naf, page, codes_postaux, tranches_effectif
                )
            except requests.HTTPError as err:
                print(f"   ⚠️ Erreur HTTP page {page} : {err}")
                break
            except requests.RequestException as err:
                print(f"   ⚠️ Erreur réseau page {page} : {err}")
                break

            resultats = data.get("results", [])
            if not resultats:
                break

            for entreprise in resultats:
                siren = entreprise.get("siren")
                if not siren:
                    continue
                entreprise["_libelle_naf_recherche"] = libelle_naf
                entreprises[siren] = entreprise

            total_naf += len(resultats)
            total_page = data.get("total_results", 0)
            print(
                f"   page {page:>2} : {len(resultats):>2} résultats "
                f"(cumulé {total_naf} / {total_page} total pour ce NAF)"
            )

            if total_naf >= total_page:
                break
            if len(resultats) < PER_PAGE:
                break

            page += 1
            time.sleep(DELAI_ENTRE_REQUETES)

    brut = list(entreprises.values())
    filtrees = [e for e in brut if siege_dans_perimetre(e, codes_postaux)]
    return filtrees, len(brut)


# --- Sauvegarde ---

def sauvegarder(
    entreprises: list[dict],
    codes_postaux: list[str],
    codes_naf: list[tuple[str, str]],
    tranches_effectif: list[str],
) -> None:
    """Écrit le résultat dans data/raw_entreprises.json."""
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "date_extraction": datetime.now(timezone.utc).isoformat(),
        "parametres": {
            "codes_postaux": codes_postaux,
            "codes_naf": [{"code": c, "libelle": l} for c, l in codes_naf],
            "tranches_effectif": tranches_effectif,
            "etat_administratif": "A",
        },
        "nombre_entreprises": len(entreprises),
        "entreprises": entreprises,
    }

    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


# --- Point d'entrée ---

def executer(
    codes_postaux: list[str] = CODES_POSTAUX,
    codes_naf: list[tuple[str, str]] = CODES_NAF,
    tranches_effectif: list[str] = TRANCHES_EFFECTIF,
) -> int:
    print("🚀 Extraction API Recherche Entreprises")
    print(f"   Codes postaux : {', '.join(codes_postaux)}")
    print(f"   Codes NAF     : {', '.join(c for c, _ in codes_naf)}")
    print(f"   Tranches eff. : {', '.join(tranches_effectif)}")

    debut = time.time()
    entreprises, total_brut = extraire_toutes(
        codes_postaux, codes_naf, tranches_effectif
    )
    duree = time.time() - debut

    sauvegarder(entreprises, codes_postaux, codes_naf, tranches_effectif)

    print(
        f"\n✅ Extraction terminée : {total_brut} entreprises uniques "
        f"remontées par l'API."
    )
    print(
        f"   Après filtre « siège actif dans le périmètre » : "
        f"{len(entreprises)} entreprises retenues."
    )
    print(f"   Durée : {duree:.1f}s")
    print(f"   Fichier : {OUTPUT_PATH.relative_to(ROOT)}")
    return 0


def main() -> int:
    return executer()


if __name__ == "__main__":
    sys.exit(main())
