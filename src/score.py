"""
Étape 4 — Scoring et classification.

V2 (par défaut) — priorité au verdict Serper :
- Serper : site détecté                                     → ÉCARTÉ
- Serper : aucun site + effectif ≥ tranche 11 (~10+ sal.)   → TRÈS PROBABLE
- Serper : aucun site + effectif < tranche 11               → PROBABLE
- Serper : entreprise non traitée (quota/limit)             → À VÉRIFIER

V1 legacy (fallback si --legacy-dns, pas de `_site` sur les entreprises) :
- DNS variantes testées, 0 résolu, effectif ≥ tranche 11 → TRÈS PROBABLE
- DNS variantes testées, 0 résolu, effectif < tranche 11 → PROBABLE
- DNS indéterminé (aucune variante générable)            → À VÉRIFIER
- DNS au moins un domaine résolu                          → ÉCARTÉ

Pappers reste désactivé par défaut pour préserver les crédits (voir README).

Entrée :
  - data/entreprises_avec_site.json   (V2, priorité)
  - data/entreprises_avec_dns.json    (fallback legacy)
Sortie : data/shortlist.json.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUT_PATH_SERPER = ROOT / "data" / "entreprises_avec_site.json"
INPUT_PATH_DNS = ROOT / "data" / "entreprises_avec_dns.json"
OUTPUT_PATH = ROOT / "data" / "shortlist.json"

# Effectif moyen par tranche INSEE (sert aussi au tri par taille)
EFFECTIF_MOYEN = {
    "01": 1.5,   "02": 4,     "03": 7.5,
    "11": 14.5,  "12": 34.5,  "21": 74.5,
    "22": 149.5, "31": 224.5, "32": 374.5,
}

# Au-dessus de cette valeur d'effectif moyen, on classe en TRÈS PROBABLE.
SEUIL_GROSSE = 10  # correspond à la tranche 11 (10-19 salariés)

# Plafond utilisé si on réactive Pappers un jour — marquage dans le shortlist.
LIMIT_TOP_PAPPERS = 80

# Scores d'affichage — pas de sémantique fine, juste pour le tri du frontend.
SCORE_AFFICHAGE = {
    "TRÈS PROBABLE": 100,
    "PROBABLE": 50,
    "À VÉRIFIER": 20,
    "ÉCARTÉ": 0,
}


def effectif_estime(entreprise: dict) -> float:
    return EFFECTIF_MOYEN.get(entreprise.get("tranche_effectif_salarie"), 0)


def classifier(entreprise: dict) -> tuple[str, int, list[dict]]:
    """Retourne (classification, score_affichage, signaux)."""
    site = entreprise.get("_site") or {}
    dns = entreprise.get("_dns") or {}
    eff = effectif_estime(entreprise)

    # --- V2 : verdict Serper prioritaire si présent ---
    if site:
        reason = site.get("reason") or ""

        # Entreprise non traitée (quota Serper épuisé ou --limit atteint).
        # On ne peut pas trancher → À VÉRIFIER plutôt qu'un faux positif.
        if reason.startswith("Non traité"):
            signal = {
                "source": "Serper",
                "verdict": "non_traite",
                "detail": reason,
            }
            classification = "À VÉRIFIER"
            return classification, SCORE_AFFICHAGE[classification] + int(eff), [signal]

        if site.get("has_site", False):
            signal = {
                "source": "Serper",
                "verdict": "site_detecte",
                "detail": f"Site trouvé : {site.get('detected_domain', '—')}",
            }
            classification = "ÉCARTÉ"
            return classification, SCORE_AFFICHAGE[classification] + int(eff), [signal]

        signal = {
            "source": "Serper",
            "verdict": "aucun_site",
            "detail": reason or "Aucun site détecté par recherche Google",
        }
        classification = "TRÈS PROBABLE" if eff >= SEUIL_GROSSE else "PROBABLE"
        return classification, SCORE_AFFICHAGE[classification] + int(eff), [signal]

    # --- V1 legacy : fallback DNS (utilisé si --legacy-dns) ---
    testes = dns.get("domaines_testes") or []
    resolus = dns.get("domaines_resolus") or []

    if resolus:
        signal = {
            "source": "DNS",
            "verdict": "domaine_resolu",
            "detail": f"{len(resolus)} domaine(s) : {', '.join(resolus)}",
        }
        classification = "ÉCARTÉ"
    elif not testes:
        signal = {
            "source": "DNS",
            "verdict": "indetermine",
            "detail": "aucune variante générable à partir du nom",
        }
        classification = "À VÉRIFIER"
    else:
        signal = {
            "source": "DNS",
            "verdict": "aucun_domaine",
            "detail": f"{len(testes)} variante(s) testée(s), aucune ne résout",
        }
        classification = "TRÈS PROBABLE" if eff >= SEUIL_GROSSE else "PROBABLE"

    # Le score d'affichage ajoute une fraction d'effectif pour trancher entre
    # deux entreprises de même classification.
    score = SCORE_AFFICHAGE[classification] + int(eff)
    return classification, score, [signal]


def detecter_mode(entreprises: list[dict]) -> str:
    """Retourne 'serper' si au moins une entreprise a un verdict _site, sinon 'dns'."""
    for e in entreprises:
        if e.get("_site"):
            return "serper"
        if e.get("_dns"):
            return "dns"
    return "serper"


def scorer(entreprises: list[dict]) -> None:
    for e in entreprises:
        classification, score, signaux = classifier(e)
        e["classification"] = classification
        e["score_affichage"] = score
        e["signaux"] = signaux


def cle_tri(e: dict) -> tuple:
    """Tri : classif (via score) desc, effectif desc, date_creation asc, siren."""
    return (
        -e["score_affichage"],
        -effectif_estime(e),
        e.get("date_creation") or "",
        e["siren"],
    )


def marquer_retenus_pappers(entreprises: list[dict], limite: int) -> None:
    """
    Flag les `limite` meilleurs (TRÈS PROBABLE + PROBABLE) comme candidats
    potentiels pour Pappers. Le flag sert uniquement si on réactive l'étape 5.
    Ignoré en V1.
    """
    candidats = [
        e for e in entreprises
        if e["classification"] in ("TRÈS PROBABLE", "PROBABLE")
    ]
    # candidats sont déjà triés (car `entreprises` a été trié par cle_tri)
    retenus = {e["siren"] for e in candidats[:limite]}
    for e in entreprises:
        e["retenu_pour_pappers"] = e["siren"] in retenus


def stats(entreprises: list[dict]) -> dict[str, int]:
    r: dict[str, int] = {}
    for e in entreprises:
        r[e["classification"]] = r.get(e["classification"], 0) + 1
    return r


def sauvegarder(entreprises: list[dict], mode: str) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    if mode == "serper":
        bareme = {
            "mode": "V2 — Serper (Google) + effectif",
            "seuil_grosse_entreprise": SEUIL_GROSSE,
            "regles": [
                "Serper : site détecté → ÉCARTÉ",
                "Serper : aucun site + effectif ≥ 10 sal. → TRÈS PROBABLE",
                "Serper : aucun site + effectif < 10 sal. → PROBABLE",
                "Serper : entreprise non traitée (quota/limit) → À VÉRIFIER",
                "(fallback V1 DNS si --legacy-dns)",
            ],
        }
    else:
        bareme = {
            "mode": "V1 legacy — DNS + effectif",
            "seuil_grosse_entreprise": SEUIL_GROSSE,
            "regles": [
                "DNS : 0 résolu + effectif ≥ 10 sal. → TRÈS PROBABLE",
                "DNS : 0 résolu + effectif < 10 sal. → PROBABLE",
                "DNS : aucune variante générable → À VÉRIFIER",
                "DNS : au moins un domaine résolu → ÉCARTÉ",
            ],
        }
    payload = {
        "date": datetime.now(timezone.utc).isoformat(),
        "bareme": bareme,
        "repartition": stats(entreprises),
        "nombre_entreprises": len(entreprises),
        "entreprises": entreprises,
    }
    with OUTPUT_PATH.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def main(legacy: bool = False) -> int:
    """
    Charge et classe les entreprises.

    `legacy=True` force la lecture de entreprises_avec_dns.json (mode V1).
    Sinon, préfère entreprises_avec_site.json (V2), fallback DNS si absent.
    """
    if legacy:
        if INPUT_PATH_DNS.exists():
            input_path = INPUT_PATH_DNS
        else:
            print(f"❌ Mode legacy demandé mais {INPUT_PATH_DNS.name} introuvable.")
            print(f"   Lance d'abord : python src/check_dns.py")
            return 1
    elif INPUT_PATH_SERPER.exists():
        input_path = INPUT_PATH_SERPER
    elif INPUT_PATH_DNS.exists():
        input_path = INPUT_PATH_DNS
    else:
        print(f"❌ Aucun fichier d'entrée trouvé.")
        print(f"   Lance d'abord : python src/detect_website.py (ou check_dns.py en legacy)")
        return 1

    with input_path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    entreprises = data["entreprises"]

    mode = detecter_mode(entreprises)
    print(f"📥 {len(entreprises)} entreprises chargées depuis {input_path.name}")
    print(f"   Mode scoring : {'V2 Serper' if mode == 'serper' else 'V1 legacy DNS'}")

    scorer(entreprises)
    entreprises.sort(key=cle_tri)
    marquer_retenus_pappers(entreprises, LIMIT_TOP_PAPPERS)
    sauvegarder(entreprises, mode)

    r = stats(entreprises)
    print("\n📊 Classification")
    for k in ("TRÈS PROBABLE", "PROBABLE", "À VÉRIFIER", "ÉCARTÉ"):
        print(f"   {k:14s} : {r.get(k, 0)}")

    tp = [e for e in entreprises if e["classification"] == "TRÈS PROBABLE"][:5]
    if tp:
        print("\n   Aperçu top 5 TRÈS PROBABLE :")
        for e in tp:
            nom = e.get("nom_complet") or e.get("nom_raison_sociale")
            tr = e.get("tranche_effectif_salarie", "?")
            print(f"   score={e['score_affichage']:>3} tr={tr} {nom}")

    print(f"\n✅ Fichier : {OUTPUT_PATH.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
