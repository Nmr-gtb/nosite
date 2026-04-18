/* ============================================================
   Fichier d'exemple non utilisé en production.
   Les vraies données sont dans un Gist privé (voir config.js).

   Ce fichier sert uniquement de référence pour comprendre le
   format attendu par le frontend : même structure que ce que
   produit `python src/main.py` dans `public/data.json`.
   ============================================================ */

window.NOSITE_DATA_EXAMPLE = {
  "generated_at": "2026-01-01T12:00:00+00:00",
  "perimetre": {
    "codes_postaux": ["06000", "06100", "06200", "06300"],
    "codes_naf": [
      { "code": "68.31Z", "libelle": "Agences immobilières" },
      { "code": "70.22Z", "libelle": "Conseil pour les affaires et autres conseils de gestion" },
      { "code": "69.10Z", "libelle": "Activités juridiques" }
    ]
  },
  "classifications": ["TRÈS PROBABLE", "PROBABLE", "À VÉRIFIER"],
  "naf_presents": [
    { "code": "68.31Z", "libelle": "Agences immobilières" },
    { "code": "69.10Z", "libelle": "Activités juridiques" },
    { "code": "70.22Z", "libelle": "Conseil pour les affaires et autres conseils de gestion" }
  ],
  "repartition": {
    "TRÈS PROBABLE": 1,
    "PROBABLE": 1,
    "À VÉRIFIER": 1,
    "ÉCARTÉ": 0
  },
  "nombre_visible": 3,
  "nombre_total": 3,
  "entreprises": [
    {
      "siren": "999000111",
      "nom": "Cabinet Dupont Test",
      "sigle": "CDT",
      "adresse": "1 rue Imaginaire 06000 NICE",
      "cp": "06000",
      "ville": "NICE",
      "lat": 43.7102,
      "lng": 7.2620,
      "naf": { "code": "69.10Z", "libelle": "Activités juridiques" },
      "effectif_tranche": "11",
      "date_creation": "2010-01-15",
      "dirigeants_recherche": [],
      "classification": "TRÈS PROBABLE",
      "score": 115,
      "dns": {
        "domaines_testes": ["cabinet-dupont-test.fr", "cabinetduponttest.com"],
        "domaines_resolus": []
      },
      "pappers": {
        "dirigeant": { "nom": "Jean Dupont", "qualite": "Gérant" },
        "telephone": null,
        "email": null
      }
    },
    {
      "siren": "999000222",
      "nom": "Agence Immo Démo",
      "sigle": "AID",
      "adresse": "25 avenue Fictive 06100 NICE",
      "cp": "06100",
      "ville": "NICE",
      "lat": 43.7180,
      "lng": 7.2750,
      "naf": { "code": "68.31Z", "libelle": "Agences immobilières" },
      "effectif_tranche": "02",
      "date_creation": "2018-06-15",
      "dirigeants_recherche": [],
      "classification": "PROBABLE",
      "score": 54,
      "dns": {
        "domaines_testes": ["agence-immo-demo.fr"],
        "domaines_resolus": []
      },
      "pappers": null
    },
    {
      "siren": "999000333",
      "nom": "Conseil Fictif SAS",
      "sigle": null,
      "adresse": "12 boulevard Inventé 06200 NICE",
      "cp": "06200",
      "ville": "NICE",
      "lat": 43.7050,
      "lng": 7.2500,
      "naf": { "code": "70.22Z", "libelle": "Conseil pour les affaires et autres conseils de gestion" },
      "effectif_tranche": "03",
      "date_creation": "2022-03-20",
      "dirigeants_recherche": [],
      "classification": "À VÉRIFIER",
      "score": 28,
      "dns": {
        "domaines_testes": [],
        "domaines_resolus": []
      },
      "pappers": null
    }
  ]
};
