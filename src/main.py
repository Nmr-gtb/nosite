"""
Étape 6 — Orchestrateur V2 du pipeline Nosite.

Enchaîne :
  1. extract.py            — API Recherche Entreprises
  2. detect_website.py     — recherche Google via Serper (défaut V2)
     ou check_dns.py       — DNS probing async (fallback via --legacy-dns)
  3. score.py              — classification selon le verdict Serper (ou DNS)
  4. (optionnel) enrich_pappers.py — seulement si --enable-pappers
  5. génération de public/data.json pour le frontend

CHANGEMENTS V2
──────────────
- Serper remplace le DNS probing comme source de vérité pour la détection.
- Le NAF 69.10Z (Activités juridiques) est exclu par défaut : les notaires
  et avocats ont systématiquement un site via leurs ordres professionnels.
  Pour le réactiver, passer `--naf 69.10Z` explicitement.
- Mode legacy DNS disponible via `--legacy-dns` au cas où Serper serait
  indisponible.

POURQUOI PAPPERS EST DÉSACTIVÉ PAR DÉFAUT
─────────────────────────────────────────
Mesuré empiriquement : un appel /v2/entreprise avec les contacts
(telephone + email) coûte ~7 jetons. Sur 80 SIREN, ça dépasse le quota
gratuit de 100. On garde les crédits en réserve pour enrichir
manuellement les leads ultra-chauds. Réactive ponctuellement avec
--enable-pappers --limit-pappers N.

USAGE
─────
  python src/main.py                             # Pipeline V2 complet (Serper)
  python src/main.py --dry-run                   # Simulation, rien n'est écrit
  python src/main.py --skip-extract              # Réutilise l'extraction
  python src/main.py --skip-extract --skip-site  # Réutilise aussi la détection
  python src/main.py --limit-serper 20           # Plafonne à 20 appels Serper
  python src/main.py --legacy-dns                # Fallback V1 (DNS)
  python src/main.py --enable-pappers --limit-pappers 5 --yes
  python src/main.py --naf 68.31Z,70.22Z         # Override des NAF

"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Par convention : désactivation par défaut. --enable-pappers renverse.
ENABLE_PAPPERS_PAR_DEFAUT = False

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
# Permet les imports `from src import ...` quand on lance `python src/main.py`
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SHORTLIST_PATH = ROOT / "data" / "shortlist.json"
PAPPERS_CACHE_PATH = ROOT / "data" / "cache" / "pappers_cache.json"
PUBLIC_DATA_PATH = ROOT / "public" / "data.json"
# data.js permet l'ouverture directe de public/index.html via file:// (double-clic),
# ce que le fetch() de data.json interdit à cause du CORS des navigateurs.
PUBLIC_DATA_JS = ROOT / "public" / "data.js"

# Villes supportées (mapping nom → codes postaux). Extensible à souhait.
VILLES_SUPPORTEES = {
    "nice": ["06000", "06100", "06200", "06300"],
    "cannes": ["06150", "06400"],
    "antibes": ["06600", "06160"],  # 06160 couvre Juan-les-Pins et Cap-d'Antibes
}

# NAF cibles, ordonnés par code et groupés par thème pour la lisibilité.
# L'ajout d'un code ne suffit PAS à le scanner : il faut qu'il corresponde
# à la cible prospection (petites entreprises sans site web évident).
NAF_LIBELLES = {
    # --- Construction / BTP ---
    "41.20A": "Construction de maisons individuelles",
    "43.21A": "Travaux d'installation électrique dans tous locaux",
    "43.22A": "Travaux d'installation d'eau et de gaz en tous locaux",
    "43.31Z": "Travaux de plâtrerie",
    "43.32A": "Travaux de menuiserie bois et PVC",
    "43.33Z": "Travaux de revêtement des sols et des murs",
    "43.34Z": "Travaux de peinture et vitrerie",
    # --- Commerce de détail ---
    "47.71Z": "Commerce de détail d'habillement en magasin spécialisé",
    "47.72A": "Commerce de détail de la chaussure",
    "47.75Z": "Commerce de détail de parfumerie et de produits de beauté en magasin spécialisé",
    "47.78C": "Autres commerces de détail spécialisés divers",
    # --- Restauration ---
    "56.10A": "Restauration traditionnelle",
    "56.10B": "Cafétérias et autres libres-services",
    "56.10C": "Restauration de type rapide",
    "56.30Z": "Débits de boissons",
    # --- Immobilier & conseil ---
    "68.31Z": "Agences immobilières",
    "69.10Z": "Activités juridiques",
    "70.22Z": "Conseil pour les affaires et autres conseils de gestion",
    # --- Beauté ---
    "96.02A": "Coiffure",
    "96.02B": "Soins de beauté",
}

# Codes NAF définitivement exclus de la cible par défaut.
# Les ordres professionnels (notaires, avocats, huissiers) couvrent
# systématiquement ces secteurs avec des sites web via leurs portails
# (notaires.fr, avocat.fr). Pour les inclure explicitement, passer
# `--naf 69.10Z` sur la ligne de commande.
NAF_EXCLUS = {"69.10Z"}

# Contacts manuellement récupérés pendant le diagnostic de l'étape 5.
# Ces valeurs ont déjà été facturées côté Pappers — on évite de les perdre.
CONTACTS_MANUELS = {
    "878379940": {
        "telephone": "0757590601",
        "email": "donnees.personnelles@lesagencesdepapa.fr",
    },
    "782612287": {"telephone": "0493165000", "email": None},
    "814003448": {"telephone": None, "email": "contact@gen-k-conseil.com"},
}


# --- Parsing des flags ---

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Orchestrateur Nosite V1 — extract → DNS → score → data.json",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("USAGE")[1] if "USAGE" in __doc__ else "",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="Simulation, aucun fichier écrit, aucun appel API.")
    p.add_argument("--skip-extract", action="store_true",
                   help="Réutilise data/raw_entreprises.json existant.")
    p.add_argument("--skip-dns", action="store_true",
                   help="(Legacy) réutilise data/entreprises_avec_dns.json existant.")
    p.add_argument("--skip-site", action="store_true",
                   help="Réutilise le fichier de détection de site existant.")
    p.add_argument("--legacy-dns", action="store_true",
                   help="Utilise la V1 (DNS probing) au lieu de Serper.")
    p.add_argument("--limit-serper", type=int, default=None,
                   help="Plafond d'appels Serper cette exécution.")
    p.add_argument(
        "--ville", default="nice",
        help=(
            "Ville(s) cible(s). Valeurs acceptées : nice, cannes, antibes, "
            "ou CSV (ex: 'nice,cannes,antibes'), ou 'all' pour tout."
        ),
    )
    p.add_argument("--naf", default=None,
                   help="CSV de codes NAF pour override (ex: '68.31Z,70.22Z').")
    p.add_argument("--enable-pappers", action="store_true",
                   help="Active l'étape Pappers (désactivée par défaut).")
    p.add_argument("--no-pappers", action="store_true",
                   help="Force la désactivation Pappers (valeur par défaut).")
    p.add_argument("--limit-pappers", type=int, default=None,
                   help="Plafond de jetons Pappers pour cette exécution.")
    p.add_argument("--yes", action="store_true",
                   help="Confirme automatiquement la consommation Pappers.")
    return p


def resoudre_codes_postaux(ville_arg: str) -> tuple[list[str], list[str]]:
    """
    Retourne (codes_postaux_union, villes_resolues).

    Accepte :
      - 'nice' → 1 ville
      - 'cannes,antibes' → plusieurs villes (union des codes postaux)
      - 'all' → toutes les villes supportées
    """
    ville_norm = ville_arg.strip().lower()

    if ville_norm == "all":
        villes_demandees = list(VILLES_SUPPORTEES.keys())
    else:
        villes_demandees = [v.strip().lower() for v in ville_norm.split(",") if v.strip()]

    invalides = [v for v in villes_demandees if v not in VILLES_SUPPORTEES]
    if invalides:
        print(f"❌ Ville(s) non supportée(s) : {', '.join(invalides)}")
        print(f"   Valeurs acceptées : {', '.join(VILLES_SUPPORTEES)} (ou 'all')")
        sys.exit(1)

    # Union des codes postaux (dédupliquée, ordre stable)
    codes_postaux: list[str] = []
    for v in villes_demandees:
        for cp in VILLES_SUPPORTEES[v]:
            if cp not in codes_postaux:
                codes_postaux.append(cp)

    return codes_postaux, villes_demandees


def resoudre_codes_naf(naf_csv: str | None) -> list[tuple[str, str]]:
    if naf_csv is None:
        # Par défaut : tous les NAF connus SAUF les exclus (ex: 69.10Z).
        return [
            (c, NAF_LIBELLES[c])
            for c in NAF_LIBELLES
            if c not in NAF_EXCLUS
        ]
    codes = [c.strip().upper() for c in naf_csv.split(",") if c.strip()]
    # Libellé inconnu → on met le code comme libellé par défaut.
    # Override explicite : on respecte le choix de l'utilisateur même pour
    # les NAF normalement exclus (ex: --naf 69.10Z réactive les juristes).
    return [(c, NAF_LIBELLES.get(c, c)) for c in codes]


# --- Génération du public/data.json pour le frontend ---

def float_safe(s) -> float | None:
    if s is None:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def charger_pappers_cache() -> dict[str, dict]:
    if not PAPPERS_CACHE_PATH.exists():
        return {}
    try:
        with PAPPERS_CACHE_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def extraire_pappers_pour_front(siren: str, cache: dict) -> dict | None:
    """Lit le cache Pappers + merge avec CONTACTS_MANUELS si disponible."""
    entree = cache.get(siren)
    data = entree.get("data") if entree else None

    dirigeant = None
    telephone = None
    email = None
    if data:
        telephone = data.get("telephone")
        email = data.get("email")
        representants = data.get("representants") or []
        for r in representants:
            if r.get("actif", True):
                nom_prenom = " ".join(
                    v for v in [r.get("prenom"), r.get("nom")] if v
                ).strip()
                dirigeant = {
                    "nom": nom_prenom or r.get("nom_complet"),
                    "qualite": r.get("qualite"),
                }
                break

    # Merge contacts manuels (crédits déjà consommés, à ne pas perdre)
    manuels = CONTACTS_MANUELS.get(siren) or {}
    telephone = telephone or manuels.get("telephone")
    email = email or manuels.get("email")

    if not (dirigeant or telephone or email):
        return None
    return {"dirigeant": dirigeant, "telephone": telephone, "email": email}


def compacter_pour_front(entreprise: dict, pappers_cache: dict) -> dict:
    """Construit la fiche minimale envoyée au frontend."""
    siege = entreprise.get("siege") or {}
    naf_code = entreprise.get("activite_principale") or ""
    naf_libelle = (
        entreprise.get("_libelle_naf_recherche")
        or NAF_LIBELLES.get(naf_code)
        or siege.get("activite_principale")
        or ""
    )
    # V2 : champ `site` si détection Serper disponible, sinon None.
    site_payload = None
    if entreprise.get("_site") is not None:
        s = entreprise.get("_site") or {}
        site_payload = {
            "has_site": s.get("has_site", False),
            "detected_url": s.get("detected_url"),
            "detected_domain": s.get("detected_domain"),
            "reason": s.get("reason"),
            "query_used": s.get("query_used"),
            "top_results_count": s.get("top_results_count", 0),
        }

    # Legacy : champ `dns` seulement si l'entreprise a été traitée via check_dns.
    dns_payload = None
    if entreprise.get("_dns") is not None:
        d = entreprise.get("_dns") or {}
        dns_payload = {
            "domaines_testes": d.get("domaines_testes") or [],
            "domaines_resolus": d.get("domaines_resolus") or [],
        }

    return {
        "siren": entreprise["siren"],
        "nom": entreprise.get("nom_complet") or entreprise.get("nom_raison_sociale"),
        "sigle": entreprise.get("sigle"),
        "adresse": siege.get("adresse"),
        "cp": siege.get("code_postal"),
        "ville": siege.get("libelle_commune"),
        "lat": float_safe(siege.get("latitude")),
        "lng": float_safe(siege.get("longitude")),
        "naf": {"code": naf_code, "libelle": naf_libelle},
        "effectif_tranche": entreprise.get("tranche_effectif_salarie"),
        "date_creation": entreprise.get("date_creation"),
        "dirigeants_recherche": entreprise.get("dirigeants") or [],
        "classification": entreprise["classification"],
        "score": entreprise["score_affichage"],
        "site": site_payload,
        "dns": dns_payload,
        "pappers": extraire_pappers_pour_front(entreprise["siren"], pappers_cache),
    }


def generer_public_data_json(
    codes_postaux: list[str],
    codes_naf: list[tuple[str, str]],
    villes_resolues: list[str],
) -> dict:
    """Lit shortlist.json + cache Pappers, écrit public/data.json."""
    if not SHORTLIST_PATH.exists():
        print(f"❌ Fichier introuvable : {SHORTLIST_PATH}")
        sys.exit(1)

    with SHORTLIST_PATH.open("r", encoding="utf-8") as f:
        shortlist = json.load(f)

    pappers_cache = charger_pappers_cache()
    entreprises = shortlist["entreprises"]

    # On exclut les ÉCARTÉ du payload frontend (moins de poids, moins de bruit).
    visibles = [e for e in entreprises if e["classification"] != "ÉCARTÉ"]
    fiches = [compacter_pour_front(e, pappers_cache) for e in visibles]

    repartition: dict[str, int] = {}
    for e in entreprises:
        c = e["classification"]
        repartition[c] = repartition.get(c, 0) + 1

    naf_filtres = sorted({f["naf"]["code"] for f in fiches if f["naf"]["code"]})
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "perimetre": {
            "villes": villes_resolues,
            "codes_postaux": codes_postaux,
            "codes_naf": [{"code": c, "libelle": l} for c, l in codes_naf],
        },
        "classifications": ["TRÈS PROBABLE", "PROBABLE", "À VÉRIFIER"],
        "naf_presents": [
            {"code": c, "libelle": NAF_LIBELLES.get(c, c)} for c in naf_filtres
        ],
        "repartition": repartition,
        "nombre_visible": len(fiches),
        "nombre_total": len(entreprises),
        "entreprises": fiches,
    }

    PUBLIC_DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    with PUBLIC_DATA_PATH.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    # Écrit aussi data.js pour permettre l'ouverture du site en double-clic
    # (file:// bloque fetch('./data.json') à cause du CORS).
    js_contenu = (
        "// Généré automatiquement par src/main.py. Ne pas éditer à la main.\n"
        "window.__NOSITE_DATA = "
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + ";\n"
    )
    with PUBLIC_DATA_JS.open("w", encoding="utf-8") as f:
        f.write(js_contenu)
    return payload


# --- Orchestration ---

def entete(titre: str) -> None:
    print(f"\n━━━ {titre} ━━━")


def main() -> int:
    args = parser().parse_args()

    if args.enable_pappers and args.no_pappers:
        print("❌ --enable-pappers et --no-pappers sont incompatibles.")
        return 1

    codes_postaux, villes_resolues = resoudre_codes_postaux(args.ville)
    codes_naf = resoudre_codes_naf(args.naf)
    pappers_active = args.enable_pappers and not args.no_pappers

    mode_detection = "V1 DNS (legacy)" if args.legacy_dns else "V2 Serper"
    print(f"🚀 Nosite pipeline V2.1 · {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   Ville(s)      : {', '.join(villes_resolues)} ({len(codes_postaux)} CP)")
    print(f"   NAF           : {', '.join(c for c, _ in codes_naf)} ({len(codes_naf)} codes)")
    print(f"   Détection     : {mode_detection}")
    print(f"   Pappers       : {'ACTIVÉ' if pappers_active else 'désactivé (défaut)'}")
    if args.dry_run:
        print("   🧪 DRY-RUN : rien ne sera écrit, aucun appel réseau.")
        return 0

    # Étape 2 — extraction
    if args.skip_extract:
        entete("Étape 2/5 · Extraction (skippée, réutilisation du fichier)")
    else:
        entete("Étape 2/5 · Extraction API Recherche Entreprises")
        from src import extract
        if extract.executer(codes_postaux, codes_naf) != 0:
            return 1

    # Étape 3 — Détection de site (Serper V2 par défaut, DNS si --legacy-dns)
    if args.legacy_dns:
        if args.skip_dns:
            entete("Étape 3/5 · DNS (skippée, mode legacy)")
        else:
            entete("Étape 3/5 · DNS probing (mode legacy V1)")
            from src import check_dns
            if check_dns.main() != 0:
                return 1
    else:
        if args.skip_site:
            entete("Étape 3/5 · Détection site (skippée)")
        else:
            entete("Étape 3/5 · Détection site web (Serper)")
            from src import detect_website
            args_site = argparse.Namespace(
                limit=args.limit_serper,
                force=False,
                dry_run=False,
            )
            if detect_website.executer(args_site) != 0:
                return 1

    # Étape 4 — scoring (passe le flag legacy pour forcer la lecture du bon fichier)
    entete("Étape 4/5 · Scoring & classification")
    from src import score
    if score.main(legacy=args.legacy_dns) != 0:
        return 1

    # Étape 5a — Pappers (optionnel)
    if pappers_active:
        entete("Étape 5a · Enrichissement Pappers (activé)")
        from src import enrich_pappers
        args_pappers = argparse.Namespace(
            limit_pappers=args.limit_pappers,
            dry_run=False,
            yes=args.yes,
        )
        if enrich_pappers.executer(args_pappers) != 0:
            return 1
    else:
        entete("Étape 5a · Pappers (désactivé — crédits préservés)")

    # Étape 5b — public/data.json
    entete("Étape 5b · Génération public/data.json")
    payload = generer_public_data_json(codes_postaux, codes_naf, villes_resolues)

    # Récap final
    r = payload["repartition"]
    print("\n📊 Récap pipeline")
    print(f"   TRÈS PROBABLE  : {r.get('TRÈS PROBABLE', 0)}")
    print(f"   PROBABLE       : {r.get('PROBABLE', 0)}")
    print(f"   À VÉRIFIER     : {r.get('À VÉRIFIER', 0)}")
    print(f"   ÉCARTÉ         : {r.get('ÉCARTÉ', 0)} (non affichées)")
    print(f"   Visibles front : {payload['nombre_visible']} / {payload['nombre_total']}")
    print(f"\n✅ public/data.json prêt — ouvre public/index.html (étape 7) pour l'utiliser.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
