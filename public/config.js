/* ============================================================
   Nosite — Configuration locale
   ============================================================

   ⚠️  REMPLACE la valeur `dataUrl` ci-dessous par l'URL "raw" de
       ton Gist privé avant d'ouvrir le site.

   Comment récupérer l'URL raw d'un Gist :
     1. Ouvre ton Gist privé sur https://gist.github.com
     2. Clique sur le bouton "Raw" en haut à droite du fichier
     3. Copie l'URL affichée dans la barre d'adresse
     4. Colle-la à la place du placeholder ci-dessous

   L'URL doit ressembler à :
     https://gist.githubusercontent.com/<pseudo>/<id-long>/raw/data.json

   Astuce : l'URL "raw" sans SHA de commit change à chaque mise à
   jour du Gist (toujours la dernière version servie). C'est le
   comportement voulu ici.

   Ce fichier est commité dans le repo PUBLIC mais ne contient
   aucune donnée sensible — juste un pointeur vers le Gist privé.
   ============================================================ */

window.NOSITE_CONFIG = {
  dataUrl: "https://gist.githubusercontent.com/Nmr-gtb/affe8ef417284690b80ad22a3dc3c1e2/raw/data.json",
};
