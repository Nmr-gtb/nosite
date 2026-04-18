"""Vérifie que le fichier .env est bien chargé et contient les clés attendues."""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv
import os

# Racine du projet (parent de src/)
ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"


def main() -> int:
    if not ENV_PATH.exists():
        print(f"❌ Fichier .env introuvable à : {ENV_PATH}")
        return 1

    load_dotenv(dotenv_path=ENV_PATH)

    insee = os.getenv("INSEE_API_KEY")
    pappers = os.getenv("PAPPERS_API_KEY")

    manquantes: list[str] = []
    if not insee:
        manquantes.append("INSEE_API_KEY")
    if not pappers:
        manquantes.append("PAPPERS_API_KEY")

    if manquantes:
        print(f"❌ Clés manquantes dans .env : {', '.join(manquantes)}")
        return 1

    # Masquer partiellement pour ne rien exposer en console
    def masquer(valeur: str) -> str:
        if len(valeur) <= 8:
            return "***"
        return f"{valeur[:4]}...{valeur[-4:]}"

    print("✅ Clés chargées")
    print(f"   INSEE_API_KEY   : {masquer(insee)}")
    print(f"   PAPPERS_API_KEY : {masquer(pappers)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
