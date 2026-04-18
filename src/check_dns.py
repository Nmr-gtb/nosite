"""
Étape 3 — DNS probing pour détecter si une entreprise a déjà un site web.

Principe :
1. Générer des variantes de domaines plausibles à partir du nom (et sigle,
   enseignes) de l'entreprise.
2. Tester chaque domaine en DNS async (A record) avec un timeout court.
3. Un domaine qui résout → signal fort que l'entreprise a un site.
4. Cache dans data/cache/dns_cache.json (TTL 30 jours).

Zéro coût, illimité. On s'en sert ensuite pour le pré-scoring avant Pappers.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import dns.asyncresolver
import dns.exception
import dns.resolver
from tqdm import tqdm

# --- Constantes ---

ROOT = Path(__file__).resolve().parent.parent
INPUT_PATH = ROOT / "data" / "raw_entreprises.json"
OUTPUT_PATH = ROOT / "data" / "entreprises_avec_dns.json"
CACHE_PATH = ROOT / "data" / "cache" / "dns_cache.json"

CACHE_TTL_JOURS = 30
DNS_TIMEOUT = 2.0            # secondes par requête
CONCURRENCE_DNS = 50         # requêtes DNS simultanées
TLD_A_TESTER = [".fr", ".com"]
LONGUEUR_MIN = 3
LONGUEUR_MAX = 45

# Formes juridiques et termes à retirer avant de générer un domaine
MOTS_VIDES = {
    "sarl", "sas", "sasu", "eurl", "snc", "sa", "sci", "scp", "selarl",
    "selas", "scm", "eirl", "gie", "scop", "societe", "sté", "ste",
    "cabinet", "groupe", "maitre", "maître", "cie", "etablissements",
    "ets", "the", "le", "la", "les", "de", "du", "des", "et", "en",
    "a", "au", "aux", "l", "d",
}

# --- Cache ---

def charger_cache() -> dict[str, dict]:
    if not CACHE_PATH.exists():
        return {}
    try:
        with CACHE_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def sauvegarder_cache(cache: dict[str, dict]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CACHE_PATH.open("w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def cache_valide(entree: dict | None) -> bool:
    if not entree or "date" not in entree:
        return False
    try:
        date = datetime.fromisoformat(entree["date"])
    except ValueError:
        return False
    age = datetime.now(timezone.utc) - date
    return age.days < CACHE_TTL_JOURS


# --- Génération de variantes de domaines ---

def normaliser(texte: str) -> list[str]:
    """Retire accents, passe en minuscules, tokenise, retire les mots vides."""
    if not texte:
        return []
    sans_accents = (
        unicodedata.normalize("NFKD", texte)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    sans_parentheses = re.sub(r"\([^)]*\)", " ", sans_accents)
    nettoye = re.sub(r"[^a-z0-9\s-]", " ", sans_parentheses)
    tokens = [t for t in re.split(r"[\s-]+", nettoye) if t]
    return [t for t in tokens if t not in MOTS_VIDES and len(t) > 1]


def variantes_pour_entreprise(entreprise: dict) -> list[str]:
    """Génère jusqu'à ~8 domaines plausibles pour une entreprise."""
    sources: list[str] = []
    if nom := entreprise.get("nom_raison_sociale"):
        sources.append(nom)
    if sigle := entreprise.get("sigle"):
        sources.append(sigle)
    siege = entreprise.get("siege") or {}
    for enseigne in siege.get("liste_enseignes") or []:
        sources.append(enseigne)

    candidats: set[str] = set()
    for source in sources:
        tokens = normaliser(source)
        if not tokens:
            continue
        compact = "".join(tokens)
        avec_tirets = "-".join(tokens)
        for forme in {compact, avec_tirets}:
            if LONGUEUR_MIN <= len(forme) <= LONGUEUR_MAX:
                for tld in TLD_A_TESTER:
                    candidats.add(f"{forme}{tld}")
    return sorted(candidats)


# --- DNS async ---

def nouveau_resolver() -> dns.asyncresolver.Resolver:
    """Resolver avec timeouts courts. Nouveau par worker pour éviter la contention."""
    resolver = dns.asyncresolver.Resolver()
    resolver.timeout = DNS_TIMEOUT
    resolver.lifetime = DNS_TIMEOUT
    return resolver


async def resoudre(resolver: dns.asyncresolver.Resolver, domaine: str) -> bool:
    """Retourne True si le domaine a au moins un A record."""
    try:
        reponse = await resolver.resolve(domaine, "A")
        return len(reponse) > 0
    except (
        dns.resolver.NXDOMAIN,
        dns.resolver.NoAnswer,
        dns.resolver.NoNameservers,
        dns.exception.Timeout,
        asyncio.TimeoutError,
        Exception,
    ):
        return False


async def tester_entreprise(
    semaphore: asyncio.Semaphore,
    entreprise: dict,
) -> tuple[str, dict]:
    """
    Teste toutes les variantes d'une entreprise.
    Retourne (siren, résultat) — on renvoie le siren pour éviter toute
    confusion d'appariement avec as_completed qui ne préserve pas l'ordre.
    """
    variantes = variantes_pour_entreprise(entreprise)
    resolus: list[str] = []

    if variantes:
        resolver = nouveau_resolver()
        async def verifier(dom: str) -> tuple[str, bool]:
            async with semaphore:
                ok = await resoudre(resolver, dom)
                return dom, ok

        resultats = await asyncio.gather(*(verifier(d) for d in variantes))
        resolus = [d for d, ok in resultats if ok]

    return entreprise["siren"], {
        "date": datetime.now(timezone.utc).isoformat(),
        "domaines_testes": variantes,
        "domaines_resolus": resolus,
        "resolu": len(resolus) > 0,
    }


# --- Orchestration ---

async def traiter(entreprises: list[dict]) -> dict[str, dict]:
    cache = charger_cache()
    semaphore = asyncio.Semaphore(CONCURRENCE_DNS)

    a_tester: list[dict] = []
    depuis_cache = 0
    for e in entreprises:
        siren = e["siren"]
        if cache_valide(cache.get(siren)):
            depuis_cache += 1
        else:
            a_tester.append(e)

    if depuis_cache:
        print(f"♻️  {depuis_cache} entreprises lues depuis le cache (< {CACHE_TTL_JOURS} j)")

    if not a_tester:
        return cache

    print(f"🌐 DNS probing sur {len(a_tester)} entreprises (concurrence {CONCURRENCE_DNS})")

    # Lancer toutes les tâches, les collecter au fur et à mesure via tqdm.
    # Chaque coroutine retourne (siren, résultat) pour éviter les erreurs
    # d'appariement (as_completed ne préserve pas l'ordre de soumission).
    taches = [tester_entreprise(semaphore, e) for e in a_tester]
    for coro in tqdm(
        asyncio.as_completed(taches), total=len(taches), unit="ent"
    ):
        siren, resultat = await coro
        cache[siren] = resultat

    sauvegarder_cache(cache)
    return cache


def enrichir_entreprises(entreprises: list[dict], cache: dict[str, dict]) -> list[dict]:
    enrichies = []
    for e in entreprises:
        siren = e["siren"]
        info = cache.get(siren, {})
        # On injecte le verdict DNS sous une clé dédiée
        e["_dns"] = {
            "domaines_testes": info.get("domaines_testes", []),
            "domaines_resolus": info.get("domaines_resolus", []),
            "resolu": info.get("resolu", False),
        }
        enrichies.append(e)
    return enrichies


def sauvegarder_enrichi(entreprises: list[dict]) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "date": datetime.now(timezone.utc).isoformat(),
        "nombre_entreprises": len(entreprises),
        "entreprises": entreprises,
    }
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def main() -> int:
    if not INPUT_PATH.exists():
        print(f"❌ Fichier introuvable : {INPUT_PATH}")
        print("   Lance d'abord : python src/extract.py")
        return 1

    with INPUT_PATH.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    entreprises = raw["entreprises"]

    print(f"📥 {len(entreprises)} entreprises chargées depuis {INPUT_PATH.name}")

    # Petites stats de variantes pour visibilité
    tailles = [len(variantes_pour_entreprise(e)) for e in entreprises]
    print(
        f"   Variantes générées : min {min(tailles)}, "
        f"max {max(tailles)}, moy {sum(tailles)/len(tailles):.1f}"
    )

    debut = time.time()
    cache = asyncio.run(traiter(entreprises))
    duree = time.time() - debut

    enrichies = enrichir_entreprises(entreprises, cache)
    sauvegarder_enrichi(enrichies)

    avec_domaine = sum(1 for e in enrichies if e["_dns"]["resolu"])
    sans_domaine = len(enrichies) - avec_domaine

    print(f"\n✅ DNS terminé en {duree:.1f}s")
    print(f"   Avec domaine résolu (probable site)  : {avec_domaine}")
    print(f"   Sans domaine résolu (prospect fort)  : {sans_domaine}")
    print(f"   Cache   : {CACHE_PATH.relative_to(ROOT)}")
    print(f"   Fichier : {OUTPUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
