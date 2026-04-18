"""
Étape 3 V2 — Détection de site web via Serper (Google Search API).

Remplace le DNS probing naïf de V1 par une vraie recherche Google. Pour
chaque entreprise, on compose une requête courte (nom ou sigle + ville),
on interroge Serper, et on décide si le top 10 des résultats contient un
site web propre à l'entreprise.

Cache 90 jours dans data/cache/serper_cache.json (les sites bougent peu).
Compteur cumulé dans data/cache/serper_compteur.json (même pattern que
Pappers) pour estimer le solde gratuit restant (2500 crédits au départ).

RÈGLES D'ACCEPTATION (un résultat Google devient « le site ») :
  1. Le domaine (hors `www.`) n'est pas dans la blacklist (brokers,
     annuaires, réseaux sociaux, ordres professionnels),
     SAUF cas spécial des sous-domaines de notaires.fr / avocat.fr qui
     sont les vrais sites des cabinets (ex: excen.notaires.fr).
  2. ET au moins une condition parmi :
     - le domaine contient un token significatif du nom (normalisé)
     - la position est 1, 2 ou 3 (top-trust Google)
     - le snippet contient l'adresse ou le code postal

GARDE-FOUS ÉCONOMIQUES (non négociables) :
  - Délai de 0.2 s entre appels (Serper free tier = 100 req/min).
  - Sauvegarde défensive du cache toutes les N=10 entreprises.
  - 401 → stop total (clé invalide).
  - 402 / 429 → stop net, sauvegarde cache + compteur.
  - 5xx / timeout → retry automatique via requests.Session (urllib3.Retry).

Options CLI :
  --limit N  : plafond d'appels Serper cette exécution
  --force    : ignore le cache, refait tous les appels
  --dry-run  : aucun appel API, affiche seulement les requêtes prévues
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from tqdm import tqdm
from urllib3.util.retry import Retry


# --- Constantes ---

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
INPUT_PATH = ROOT / "data" / "raw_entreprises.json"
OUTPUT_PATH = ROOT / "data" / "entreprises_avec_site.json"
CACHE_PATH = ROOT / "data" / "cache" / "serper_cache.json"
COMPTEUR_PATH = ROOT / "data" / "cache" / "serper_compteur.json"

API_URL = "https://google.serper.dev/search"
QUOTA_GRATUIT_INITIAL = 2500       # informatif, affichage uniquement
CACHE_TTL_JOURS = 90
DELAI_ENTRE_APPELS = 0.2           # 100 req/min sur le free tier Serper
SAUVEGARDE_TOUS_LES_N = 10         # sauvegarde défensive du cache
TIMEOUT_SERPER = 10                # secondes

# Blacklist : domaines (et tous leurs sous-domaines) qui ne sont JAMAIS
# le site officiel d'une entreprise. Le test est un suffix-match : `kompass.com`
# rejette aussi `fr.kompass.com`, `entreprises.lefigaro.fr`, etc.
# Exception : les sous-domaines légitimes listés dans
# PORTAILS_AVEC_SOUS_DOMAINES_LEGITIMES restent acceptés.
DOMAINES_BROKERS = {
    # Data brokers / registres
    "pappers.fr", "societe.com", "societeinfo.com", "infogreffe.fr",
    "verif.com", "bilans-gratuits.fr", "sirene.fr", "manageo.fr", "score3.fr",
    "corporama.com", "dirigeant.com", "rcs-national.com",
    "entreprises-et-societes.fr", "lefigaro.fr", "lagazettefrance.fr",
    "kompass.com", "europages.fr", "trouverunesociete.com", "ellisphere.com",
    "bodacc.fr", "infonet.fr", "cartesfrance.fr", "scores-decisions.com",
    # Annuaires généralistes
    "pagesjaunes.fr", "118218.fr", "118000.fr", "yelp.fr", "yelp.com",
    "tripadvisor.fr", "tripadvisor.com",
    "google.com", "google.fr", "bing.com", "waze.com",
    # Réseaux sociaux
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com",
    "youtube.com", "tiktok.com", "pinterest.fr", "pinterest.com",
    # Ordres professionnels & annuaires métier (portails, pas site propre)
    "notaires.fr", "avocat.fr", "village-justice.com",
    "annuaire-notaires.com", "immonot.com", "encheres-publiques.com",
    "cnhj.fr", "ordre-medecins.fr",
    # Agrégateurs immo (pas le site d'une agence propre)
    "seloger.com", "leboncoin.fr", "logic-immo.com", "bienici.com",
    "pap.fr", "avendrealouer.fr", "fnaim.fr", "century21.fr",
    # Médias et actualités
    "lesechos.fr", "lemonde.fr", "bfmtv.com", "20minutes.fr", "ouest-france.fr",
    # Emploi
    "indeed.fr", "indeed.com", "hellowork.com", "glassdoor.fr", "glassdoor.com",
    "welcometothejungle.com",
}

# Portails ordres pros : les SOUS-domaines sont les vrais sites des cabinets
# (ex: excen.notaires.fr, maitre-dupont.avocat.fr). On accepte tout
# sous-domaine sauf `www`.
PORTAILS_AVEC_SOUS_DOMAINES_LEGITIMES = {"notaires.fr", "avocat.fr"}

# Suffixes blacklistés : tout sous-domaine de ces racines est rejeté.
# Cas observé en avril 2026 : `annuaire-entreprises.data.gouv.fr` apparaît
# en position 1 pour quasiment toutes les entreprises françaises (registre
# SIRENE officiel) → sans cette règle, on écarte tout par erreur.
SUFFIXES_BROKERS = {"data.gouv.fr"}

# Formes juridiques et mots vides à retirer lors de la tokenisation du nom.
# Sert à décider si un domaine « ressemble » au nom de l'entreprise.
MOTS_VIDES = {
    "sarl", "sas", "sasu", "eurl", "snc", "sa", "sci", "scp", "selarl",
    "selas", "scm", "eirl", "gie", "scop", "societe", "ste", "sté",
    "cabinet", "groupe", "maitre", "maître", "cie", "etablissements",
    "ets", "the", "le", "la", "les", "de", "du", "des", "et", "en",
    "a", "au", "aux", "l", "d",
}


# --- Exceptions ---

class SerperQuotaError(Exception):
    """Quota Serper épuisé (HTTP 402) ou rate limit (HTTP 429)."""


class SerperAuthError(Exception):
    """Clé API Serper invalide (HTTP 401)."""


# --- I/O cache & compteur ---

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


# --- Normalisation ---

def normaliser(texte: str) -> list[str]:
    """Retire accents, lowercase, retire formes juridiques et mots vides."""
    if not texte:
        return []
    sans_accents = (
        unicodedata.normalize("NFKD", texte)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    nettoye = re.sub(r"[^a-z0-9\s-]", " ", sans_accents)
    tokens = [t for t in re.split(r"[\s-]+", nettoye) if t]
    return [t for t in tokens if t not in MOTS_VIDES and len(t) > 1]


# --- Construction de la requête Google ---

def construire_query(entreprise: dict) -> str:
    """
    Construit la requête Google en 2 composants :
      1. Nom principal (sigle court privilégié, sinon nom tronqué à 80 car.)
      2. Ville (siege.libelle_commune) pour le contexte géographique
    """
    nom = entreprise.get("nom_complet") or entreprise.get("nom_raison_sociale") or ""
    sigle = entreprise.get("sigle") or ""
    siege = entreprise.get("siege") or {}
    ville = siege.get("libelle_commune") or ""

    if sigle and len(sigle) < 30 and sigle.upper() != nom.upper():
        partie_nom = sigle
    else:
        partie_nom = nom[:80]

    parts = [p for p in (partie_nom, ville) if p]
    return " ".join(parts).strip()


# --- Session Serper ---

def creer_session() -> requests.Session:
    """Session avec retry automatique sur 5xx et timeouts réseau."""
    session = requests.Session()
    retry = Retry(
        total=3,
        backoff_factor=2,                       # 0 s, 2 s, 4 s
        status_forcelist=(500, 502, 503, 504, 408),
        allowed_methods=("POST",),
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "nosite/2.0 (detection site web)"})
    return session


def appeler_serper(session: requests.Session, query: str, api_key: str) -> dict:
    """
    Un appel Serper = 1 crédit.

    Gestion des statuts :
      - 401            : SerperAuthError (clé invalide, stop total)
      - 402, 429       : SerperQuotaError (quota/rate limit, stop net)
      - 5xx, timeout   : retry automatique via Session (cf. Retry config)
    """
    response = session.post(
        API_URL,
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
        json={"q": query, "gl": "fr", "hl": "fr", "num": 10},
        timeout=TIMEOUT_SERPER,
    )
    if response.status_code == 401:
        raise SerperAuthError("Clé SERPER_API_KEY invalide (HTTP 401).")
    if response.status_code in (402, 429):
        raise SerperQuotaError(
            f"Quota Serper épuisé ou rate limit (HTTP {response.status_code})."
        )
    response.raise_for_status()
    return response.json()


# --- Parsing & règle d'acceptation ---

def extraire_domaine(url: str) -> str:
    """Retourne le hostname en minuscule, sans le préfixe 'www.'."""
    if not url:
        return ""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def est_blacklist(domaine: str) -> bool:
    """
    True si le domaine est blacklisté. Exception pour les sous-domaines
    légitimes de notaires.fr / avocat.fr (sites de cabinets hébergés sur
    le portail professionnel — ex: excen.notaires.fr).
    """
    if not domaine:
        return True
    # 1. Sous-domaine d'un portail légitime → accepté (retour False immédiat)
    for portail in PORTAILS_AVEC_SOUS_DOMAINES_LEGITIMES:
        if domaine.endswith("." + portail):
            prefix = domaine[: -(len(portail) + 1)]
            if prefix and prefix != "www":
                return False
    # 2. Suffixe registre (ex: *.data.gouv.fr) → rejeté
    for suffix in SUFFIXES_BROKERS:
        if domaine == suffix or domaine.endswith("." + suffix):
            return True
    # 3. Blacklist en suffix-match : `kompass.com` rejette aussi `fr.kompass.com`
    for root in DOMAINES_BROKERS:
        if domaine == root or domaine.endswith("." + root):
            return True
    return False


def domaine_contient_token(domaine: str, tokens_nom: list[str]) -> bool:
    """
    True si la partie locale du domaine contient au moins un token
    significatif (3+ caractères) du nom de l'entreprise. Les tirets sont
    retirés avant comparaison (ex: 'jvc-consulting' → 'jvcconsulting').
    """
    if not tokens_nom or not domaine:
        return False
    cible = domaine.split(".")[0].replace("-", "")
    return any(len(t) >= 3 and t in cible for t in tokens_nom)


def snippet_contient_adresse(snippet: str, entreprise: dict) -> bool:
    """True si le snippet Google contient le CP ou un fragment de voie."""
    if not snippet:
        return False
    siege = entreprise.get("siege") or {}
    s = snippet.lower()
    cp = siege.get("code_postal") or ""
    if cp and cp in s:
        return True
    voie = (siege.get("libelle_voie") or "").lower().strip()
    if voie and len(voie) >= 6 and voie in s:
        return True
    return False


def evaluer_resultats(
    organic: list[dict],
    entreprise: dict,
) -> tuple[dict | None, list[dict]]:
    """
    Parcourt le top 10 Google. Retourne (verdict, top_results_decoree).

    verdict           : dict {url, domain, position, reason} si un résultat
                        est accepté, sinon None.
    top_results       : chaque résultat avec `rejected_reason` (None si
                        accepté, sinon raison courte du rejet pour debug).
    """
    nom = entreprise.get("nom_complet") or entreprise.get("nom_raison_sociale") or ""
    tokens_nom = normaliser(nom)

    decoree: list[dict] = []
    verdict: dict | None = None

    for r in organic:
        position = r.get("position")
        link = r.get("link") or ""
        title = r.get("title") or ""
        snippet = r.get("snippet") or ""
        domaine = extraire_domaine(link)

        entry = {
            "position": position,
            "title": title,
            "link": link,
            "domain": domaine,
            "rejected_reason": None,
        }

        if verdict is not None:
            # On a déjà accepté un résultat plus haut ; on marque les suivants
            # comme non évalués (utile pour le débogage et le cache).
            entry["rejected_reason"] = "non_evalue_deja_accepte"
            decoree.append(entry)
            continue

        if est_blacklist(domaine):
            entry["rejected_reason"] = "blacklist"
            decoree.append(entry)
            continue

        match_token = domaine_contient_token(domaine, tokens_nom)
        top_position = isinstance(position, int) and position <= 3
        match_adresse = snippet_contient_adresse(snippet, entreprise)

        if match_token or top_position or match_adresse:
            raisons = []
            if match_token:
                raisons.append("nom match")
            if top_position:
                raisons.append(f"top {position}")
            if match_adresse:
                raisons.append("adresse match")
            verdict = {
                "url": link,
                "domain": domaine,
                "position": position,
                "reason": (
                    f"Domaine valide trouvé en position {position} "
                    f"({', '.join(raisons)})"
                ),
            }
            decoree.append(entry)
        else:
            entry["rejected_reason"] = "pas_de_correspondance"
            decoree.append(entry)

    return verdict, decoree


# --- Fonction publique principale ---

def detecter_site(
    entreprise: dict,
    cache: dict,
    api_key: str,
    session: requests.Session,
    dry_run: bool = False,
) -> dict:
    """
    Détecte le site web d'une entreprise. Retourne un dict `verdict` avec
    la structure stockée dans le cache :
      {siren, date, query, has_site, detected_url, detected_domain,
       reason, top_results}

    - Consulte le cache en premier (TTL 90 j). Hit → pas d'appel API.
    - En mode `dry_run`, retourne un verdict synthétique sans appeler l'API
      et sans modifier le cache.
    - Met à jour le cache en place (le caller est responsable de la
      persistance sur disque).
    """
    siren = entreprise["siren"]
    if cache_valide(cache.get(siren)):
        return cache[siren]

    query = construire_query(entreprise)

    if dry_run:
        return {
            "siren": siren,
            "date": datetime.now(timezone.utc).isoformat(),
            "query": query,
            "has_site": False,
            "detected_url": None,
            "detected_domain": None,
            "reason": "dry-run — aucun appel API effectué",
            "top_results": [],
        }

    data = appeler_serper(session, query, api_key)
    organic = data.get("organic") or []
    verdict, top_results = evaluer_resultats(organic, entreprise)

    if verdict is not None:
        has_site = True
        detected_url = verdict["url"]
        detected_domain = verdict["domain"]
        reason = verdict["reason"]
    else:
        has_site = False
        detected_url = None
        detected_domain = None
        reason = (
            "Aucun résultat pertinent dans le top 10"
            if organic
            else "Aucun résultat Google retourné"
        )

    verdict_final = {
        "siren": siren,
        "date": datetime.now(timezone.utc).isoformat(),
        "query": query,
        "has_site": has_site,
        "detected_url": detected_url,
        "detected_domain": detected_domain,
        "reason": reason,
        "top_results": top_results,
    }
    cache[siren] = verdict_final
    return verdict_final


# --- Injection dans le pipeline ---

def injecter_verdict(entreprise: dict, verdict: dict) -> None:
    """Ajoute le champ _site à l'entreprise (forme compacte, sans top_results)."""
    top_results = verdict.get("top_results") or []
    rejected = [r for r in top_results if r.get("rejected_reason")]
    entreprise["_site"] = {
        "has_site": verdict.get("has_site", False),
        "detected_url": verdict.get("detected_url"),
        "detected_domain": verdict.get("detected_domain"),
        "reason": verdict.get("reason"),
        "query_used": verdict.get("query"),
        "top_results_count": len(top_results),
        "rejected_count": len(rejected),
    }


# --- Orchestration ---

def charger_api_key() -> str:
    load_dotenv(dotenv_path=ENV_PATH)
    key = os.getenv("SERPER_API_KEY")
    if not key:
        print("❌ SERPER_API_KEY manquante dans .env")
        sys.exit(1)
    return key


def executer(args: argparse.Namespace) -> int:
    """
    Lit raw_entreprises.json, appelle Serper pour chaque entreprise sans
    cache valide (ou toutes si --force), écrit data/entreprises_avec_site.json
    avec un champ `_site` sur chaque entreprise.

    Retourne 0 si succès, 1 sinon.
    """
    if not INPUT_PATH.exists():
        print(f"❌ Fichier introuvable : {INPUT_PATH}")
        print("   Lance d'abord : python src/extract.py")
        return 1

    raw = charger_json(INPUT_PATH, {})
    entreprises: list[dict] = raw.get("entreprises") or []
    if not entreprises:
        print("❌ Aucune entreprise à traiter dans raw_entreprises.json")
        return 1

    cache = charger_json(CACHE_PATH, {})
    compteur = charger_json(COMPTEUR_PATH, {"total_consomme": 0})
    consomme_cumule = compteur.get("total_consomme", 0)
    solde_estime = max(QUOTA_GRATUIT_INITIAL - consomme_cumule, 0)

    print(
        f"🌐 Détection de site web via Serper "
        f"(compte : ~{solde_estime} crédits estimés restants sur {QUOTA_GRATUIT_INITIAL})"
    )

    # Sélection des entreprises à appeler (cache invalide ou --force)
    force = getattr(args, "force", False)
    a_traiter: list[dict] = []
    depuis_cache = 0
    for e in entreprises:
        if not force and cache_valide(cache.get(e["siren"])):
            depuis_cache += 1
        else:
            a_traiter.append(e)

    # --limit : plafond d'appels Serper cette exécution
    limit = getattr(args, "limit", None)
    if limit is not None and limit < len(a_traiter):
        a_traiter = a_traiter[:limit]
        print(f"🎚️  Plafond --limit : {limit} appel(s) max cette exécution")

    print(
        f"📥 {len(entreprises)} entreprise(s) à traiter "
        f"({depuis_cache} depuis le cache)"
    )

    if getattr(args, "dry_run", False):
        print("\n🧪 --dry-run : aucun appel API ne sera effectué.")
        print(f"   Appels qui seraient faits : {len(a_traiter)}")
        for e in a_traiter[:5]:
            q = construire_query(e)
            print(f"     → SIREN {e['siren']} : query=\"{q}\"")
        if len(a_traiter) > 5:
            print(f"     (... et {len(a_traiter) - 5} autres)")
        return 0

    credits_cette_exec = 0
    stop_net = False
    if a_traiter:
        api_key = charger_api_key()
        session = creer_session()

        try:
            iterator = tqdm(a_traiter, unit="ent", desc="Serper")
            for i, e in enumerate(iterator, 1):
                try:
                    detecter_site(e, cache, api_key, session, dry_run=False)
                    credits_cette_exec += 1
                except SerperAuthError as err:
                    print(f"\n🛑 {err}")
                    print("   Vérifie ta clé dans .env puis relance.")
                    return 1
                except SerperQuotaError as err:
                    print(f"\n🛑 {err}")
                    print(
                        "   Cache et compteur sauvegardés. Relance plus tard "
                        "ou recharge ton compte Serper."
                    )
                    stop_net = True
                    break
                except requests.RequestException as err:
                    print(
                        f"\n⚠️  Erreur pour SIREN {e['siren']} : {err} "
                        "— on continue."
                    )
                # Sauvegarde défensive tous les N appels (contre un crash)
                if i % SAUVEGARDE_TOUS_LES_N == 0:
                    sauvegarder_json(CACHE_PATH, cache)
                time.sleep(DELAI_ENTRE_APPELS)
        finally:
            # Sauvegarde défensive systématique, même en cas d'exception
            sauvegarder_json(CACHE_PATH, cache)
            compteur["total_consomme"] = consomme_cumule + credits_cette_exec
            compteur["derniere_execution"] = datetime.now(timezone.utc).isoformat()
            sauvegarder_json(COMPTEUR_PATH, compteur)
    else:
        print("✨ Tout est en cache — aucun crédit à consommer.")

    # Injection du verdict dans chaque entreprise (forme compacte)
    avec_site = 0
    sans_site = 0
    non_traite = 0
    for e in entreprises:
        entree = cache.get(e["siren"])
        if entree:
            injecter_verdict(e, entree)
            if entree.get("has_site"):
                avec_site += 1
            else:
                sans_site += 1
        else:
            # Cas rare : arrêt précoce (quota) avant de traiter cette entreprise.
            # On injecte un verdict vide pour cohérence du payload aval.
            injecter_verdict(e, {
                "has_site": False,
                "detected_url": None,
                "detected_domain": None,
                "reason": "Non traité (arrêt précoce, quota ou --limit)",
                "query": construire_query(e),
                "top_results": [],
            })
            non_traite += 1

    total_cumule = compteur.get("total_consomme", 0)
    solde_restant = max(QUOTA_GRATUIT_INITIAL - total_cumule, 0)

    payload = {
        "date": datetime.now(timezone.utc).isoformat(),
        "nombre_entreprises": len(entreprises),
        "credits_consommes_cette_execution": credits_cette_exec,
        "credits_total_consommes_cumule": total_cumule,
        "credits_estimes_restants": solde_restant,
        "entreprises": entreprises,
    }
    sauvegarder_json(OUTPUT_PATH, payload)

    print("\n📊 Résultats :")
    print(f"   Avec site détecté (ÉCARTÉ)         : {avec_site}")
    print(f"   Sans site (PROSPECT qualifié)      : {sans_site}")
    if non_traite:
        print(f"   Non traitées (quota/limit)         : {non_traite}")
    print(f"   Crédits consommés cette exécution  : {credits_cette_exec}")
    print(
        f"   Crédits estimés restants           : "
        f"{solde_restant} / {QUOTA_GRATUIT_INITIAL}"
    )
    print(f"\n✅ Fichier : {OUTPUT_PATH.relative_to(ROOT)}")

    return 0 if not stop_net else 1


def parser_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Détection de site web via Serper (Google Search API)."
    )
    p.add_argument(
        "--limit", type=int, default=None,
        help="Plafond d'appels Serper cette exécution.",
    )
    p.add_argument(
        "--force", action="store_true",
        help="Ignore le cache, refait tous les appels (coûteux).",
    )
    p.add_argument(
        "--dry-run", action="store_true",
        help="Simulation, aucun appel API.",
    )
    return p.parse_args()


if __name__ == "__main__":
    sys.exit(executer(parser_args()))
