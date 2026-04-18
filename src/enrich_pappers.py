"""
Étape 5 — Enrichissement Pappers (DÉSACTIVÉ PAR DÉFAUT en V1).

Ce module n'impose PAS de barème — il injecte seulement les données Pappers
(dirigeant, téléphone, email) sur les entreprises retenues. La classification
reste celle produite par `score.py` (DNS + effectif).

COÛT RÉEL MESURÉ (avril 2026) :
- /v2/entreprise sans champs supplémentaires : 1 jeton
- /v2/entreprise?champs_supplementaires=telephone,email : 7 jetons
- Paramètre `site_web` non reconnu par l'API (la réponse n'expose aucun champ
  site/url/web, même en le demandant). On ne le demande donc pas.

RÈGLES D'ÉCONOMIE (non négociables) :
- Jamais plus de LIMIT_PAPPERS appels par exécution.
- Cache systématique (TTL 30 j) — on ne recrédite jamais un SIREN déjà vu.
- Confirmation interactive avant consommation, sauf --yes.
- Compteur cumulé persistant pour estimer le solde restant.

Options CLI :
  --limit-pappers N : plafond manuel (défaut : LIMIT_PAPPERS)
  --dry-run         : simulation, aucun appel API
  --yes             : skip la confirmation interactive (utile en orchestration)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from urllib3.util.retry import Retry

# --- Constantes ---

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
INPUT_PATH = ROOT / "data" / "shortlist.json"
OUTPUT_PATH = ROOT / "data" / "scored_final.json"
CACHE_PATH = ROOT / "data" / "cache" / "pappers_cache.json"
COMPTEUR_PATH = ROOT / "data" / "cache" / "pappers_compteur.json"

API_URL = "https://api.pappers.fr/v2/entreprise"
SUIVI_URL = "https://api.pappers.fr/v2/suivi-jetons"
QUOTA_GRATUIT_INITIAL = 100   # informatif, affichage uniquement
LIMIT_PAPPERS = 40            # plafond par exécution (prudent au départ)
CACHE_TTL_JOURS = 30
DELAI_ENTRE_APPELS = 0.4      # bon citoyen

# Champs supplémentaires à demander. Chaque champ = ~3 jetons supplémentaires.
# Total : 1 (base) + 3 (telephone) + 3 (email) = 7 jetons / appel.
# `site_web` volontairement absent (non supporté par l'API en pratique).
CHAMPS_SUPPLEMENTAIRES = "telephone,email"


# --- Cache & compteur ---

def charger_json(chemin: Path, defaut):
    if not chemin.exists():
        return defaut
    try:
        with chemin.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return defaut


def sauvegarder_json(chemin: Path, obj) -> None:
    chemin.parent.mkdir(parents=True, exist_ok=True)
    with chemin.open("w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def cache_valide(entree: dict | None) -> bool:
    if not entree or "date" not in entree:
        return False
    try:
        date = datetime.fromisoformat(entree["date"])
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - date).days < CACHE_TTL_JOURS


# --- Session Pappers ---

def creer_session() -> requests.Session:
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


class PappersQuotaError(Exception):
    """Quota de crédits Pappers épuisé (HTTP 402)."""


def appeler_pappers(session: requests.Session, siren: str, api_key: str) -> dict:
    """Un appel = ~7 crédits (1 base + 3 telephone + 3 email)."""
    params = {
        "api_token": api_key,
        "siren": siren,
        "champs_supplementaires": CHAMPS_SUPPLEMENTAIRES,
    }
    reponse = session.get(API_URL, params=params, timeout=15)
    if reponse.status_code == 402:
        raise PappersQuotaError()
    reponse.raise_for_status()
    return reponse.json()


def solde_reel(api_key: str) -> int | None:
    """Interroge /v2/suivi-jetons (gratuit) pour le vrai solde. None si KO."""
    try:
        r = requests.get(SUIVI_URL, params={"api_token": api_key}, timeout=10)
        if r.status_code == 200:
            return r.json().get("jetons_pay_as_you_go_restants")
    except requests.RequestException:
        pass
    return None


# --- Extraction des champs utiles depuis la réponse Pappers ---

def extraire_contacts(pappers: dict) -> dict:
    """Téléphone + email (présents si champs_supplementaires=telephone,email)."""
    return {
        "telephone": pappers.get("telephone"),
        "email": pappers.get("email"),
    }


def extraire_dirigeant_principal(pappers: dict) -> dict | None:
    """Premier représentant actif (gratuit — toujours présent)."""
    representants = pappers.get("representants") or []
    for r in representants:
        if r.get("actif", True):
            nom_prenom = " ".join(
                v for v in [r.get("prenom"), r.get("nom")] if v
            ).strip()
            return {
                "nom": nom_prenom or r.get("nom_complet"),
                "qualite": r.get("qualite"),
            }
    return None


def injecter_pappers(entreprise: dict, pappers_data: dict | None) -> None:
    """Ajoute entreprise['pappers'] = {...} sans toucher au scoring."""
    if pappers_data is None:
        entreprise["pappers"] = None
        return
    contacts = extraire_contacts(pappers_data)
    entreprise["pappers"] = {
        "telephone": contacts["telephone"],
        "email": contacts["email"],
        "dirigeant": extraire_dirigeant_principal(pappers_data),
    }


# --- Confirmation interactive ---

def demander_confirmation(
    nb_appels: int,
    cout_estime: int,
    consomme_cumule: int,
    auto_yes: bool,
) -> bool:
    print("\n" + "=" * 60)
    print(f"💳 {nb_appels} appel(s) Pappers à faire (~{cout_estime} jetons estimés).")
    print(f"   Cumul historique script : {consomme_cumule} jeton(s)")
    restant_estime = max(QUOTA_GRATUIT_INITIAL - consomme_cumule - cout_estime, 0)
    print(
        f"   Quota gratuit initial : {QUOTA_GRATUIT_INITIAL}. "
        f"Restant estimé APRÈS : {restant_estime}"
    )
    print("=" * 60)
    if auto_yes:
        print("   --yes : confirmation automatique.")
        return True
    reponse = input("Confirmer la consommation ? (oui/non) : ").strip().lower()
    return reponse in {"oui", "o", "yes", "y"}


# --- Orchestration ---

def charger_api_key() -> str:
    load_dotenv(dotenv_path=ENV_PATH)
    key = os.getenv("PAPPERS_API_KEY")
    if not key:
        print("❌ PAPPERS_API_KEY manquante dans .env")
        sys.exit(1)
    return key


def executer(args) -> int:
    if not INPUT_PATH.exists():
        print(f"❌ Fichier introuvable : {INPUT_PATH}")
        print("   Lance d'abord : python src/score.py")
        return 1

    shortlist = charger_json(INPUT_PATH, {})
    entreprises: list[dict] = shortlist["entreprises"]

    # Les retenus viennent du flag `retenu_pour_pappers` (plafond déjà appliqué
    # à l'étape 4). On applique ici un second plafond, volontairement plus bas
    # par défaut (LIMIT_PAPPERS=40) pour préserver les crédits au démarrage.
    # --limit-pappers override la constante si fourni.
    retenus_etape4 = [e for e in entreprises if e.get("retenu_pour_pappers")]
    plafond = args.limit_pappers if args.limit_pappers is not None else LIMIT_PAPPERS
    if plafond < len(retenus_etape4):
        # On prend les N premiers (déjà triés par score à l'étape 4)
        retenus_etape4 = retenus_etape4[:plafond]
    print(f"🎚️  Plafond Pappers pour cette exécution : {plafond}")

    cache = charger_json(CACHE_PATH, {})
    compteur = charger_json(COMPTEUR_PATH, {"total_consomme": 0})

    # Déterminer qui nécessite un vrai appel
    a_appeler: list[dict] = []
    depuis_cache = 0
    for e in retenus_etape4:
        if cache_valide(cache.get(e["siren"])):
            depuis_cache += 1
        else:
            a_appeler.append(e)

    print(f"📥 {len(entreprises)} entreprises chargées "
          f"(dont {len(retenus_etape4)} retenues pour Pappers)")
    if depuis_cache:
        print(f"♻️  {depuis_cache} déjà en cache (< {CACHE_TTL_JOURS} j) — pas de recrédit")

    if args.dry_run:
        print(f"\n🧪 --dry-run : aucun appel API ne sera effectué.")
        print(f"   Appels qui seraient faits : {len(a_appeler)}")
        return 0

    credits_cette_exec = 0
    if a_appeler:
        # Un appel coûte ~7 jetons (1 base + 3 telephone + 3 email).
        cout_estime = len(a_appeler) * 7
        if not demander_confirmation(
            nb_appels=len(a_appeler),
            cout_estime=cout_estime,
            consomme_cumule=compteur.get("total_consomme", 0),
            auto_yes=args.yes,
        ):
            print("⏸️  Annulé par l'utilisateur.")
            return 0

        api_key = charger_api_key()
        session = creer_session()

        print(f"\n🔌 Appels Pappers en cours ({len(a_appeler)}, ~{cout_estime} jetons)")
        try:
            for e in tqdm(a_appeler, unit="ent"):
                try:
                    data = appeler_pappers(session, e["siren"], api_key)
                    cache[e["siren"]] = {
                        "date": datetime.now(timezone.utc).isoformat(),
                        "data": data,
                    }
                    credits_cette_exec += 7   # estimation, vérifiée ensuite
                except PappersQuotaError:
                    print("\n🛑 Quota Pappers épuisé (HTTP 402) — on s'arrête là.")
                    break
                except requests.RequestException as err:
                    print(f"\n⚠️  Erreur pour SIREN {e['siren']} : {err} — on continue.")
                time.sleep(DELAI_ENTRE_APPELS)
        finally:
            # Sauvegarde défensive même si ça crashe en cours de route
            sauvegarder_json(CACHE_PATH, cache)
            compteur["total_consomme"] = compteur.get("total_consomme", 0) + credits_cette_exec
            compteur["derniere_execution"] = datetime.now(timezone.utc).isoformat()
            sauvegarder_json(COMPTEUR_PATH, compteur)
    else:
        print("✨ Tout est en cache — aucun crédit à consommer.")

    # --- Injection Pappers (sans toucher à classification/score) ---
    for e in entreprises:
        entree = cache.get(e["siren"])
        pappers_data = entree["data"] if cache_valide(entree) else None
        injecter_pappers(e, pappers_data)

    # Solde réel (interrogeable gratuitement)
    api_key = os.getenv("PAPPERS_API_KEY")
    solde = solde_reel(api_key) if api_key else None

    payload = {
        "date": datetime.now(timezone.utc).isoformat(),
        "credits_consommes_cette_execution": credits_cette_exec,
        "credits_total_consommes_cumule": compteur.get("total_consomme", 0),
        "credits_restants_reels_pappers": solde,
        "nombre_entreprises": len(entreprises),
        "nombre_enrichies": sum(1 for e in entreprises if e.get("pappers")),
        "entreprises": entreprises,
    }
    sauvegarder_json(OUTPUT_PATH, payload)

    print(f"\n💳 Crédits consommés cette exécution : ~{credits_cette_exec}")
    print(f"   Cumul historique script             : {compteur.get('total_consomme', 0)}")
    if solde is not None:
        print(f"   Solde réel Pappers (API /suivi-jetons) : {solde}")
    print(f"\n✅ Fichier : {OUTPUT_PATH.relative_to(ROOT)}")
    return 0


def parser_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Enrichissement Pappers avec garde-fous.")
    p.add_argument("--limit-pappers", type=int, default=None,
                   help="Plafond manuel de crédits pour cette exécution.")
    p.add_argument("--dry-run", action="store_true",
                   help="Simulation, aucun appel API.")
    p.add_argument("--yes", action="store_true",
                   help="Confirme automatiquement (utile en orchestration).")
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(executer(parser_args()))
