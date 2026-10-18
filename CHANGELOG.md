# Journal des versions — News Bot

Format : `## x.y — date` (ou `## x.y.z` pour une version qui ne contient que des **corrections de bugs**). **x** change quand l'application change profondément,
**y** à chaque lot de nouveautés mis en service, **z** à chaque version de corrections. Les nouveautés en cours de développement ne sont pas listées ici tant qu'elles sont
en mode bêta (Admin → Bêta) ; elles entrent dans la version qui les met en service. (Historique reconstitué le 2026-10-18 à partir des dates du dépôt.)

## 1.3 — 2026-10-18
- **Admin** : un onglet regroupe Settings, Debug, **Bêta** (mode bêta, version, journal des versions) et **Correctifs** (versions de correction x.y.z, procédure, retour arrière).
- Téléphone : **menu en bas de l'écran** (Feed, Favorites, Jobs, Sources, Plus) à la place de la barre d'onglets qui débordait de l'écran.
- Accessibilité : champs de recherche et de groupe nommés pour les lecteurs d'écran ; numéro de version visible dans Admin.

## 1.2 — 2026-10-09
- Jobs : offres de thèse et d'emploi (dates limites, calendrier, étiquettes, archivage, flux RSS), source app.doctorat.gouv.fr, correction des doublons Indeed dans un même digest.
- Les fichiers de l'interface sont rechargés après chaque mise à jour (plus de version périmée en cache).

## 1.1 — 2026-08-24
- Onglet **Debug** : état en direct du planificateur, du bot Discord et du scraping, source par source.
- Sélecteur de zones de page pour ajouter une source, flux RSS agrégé.

## 1.0 — 2026-08-18
- **Application locale permanente** : base SQLite, interface web (Feed, Favorites, Sources, Settings), favoris et dossiers, newsletter par email, sources modifiables, santé des sources, étiquettes de sujet,
  rattrapage d'un digest manqué.

## 0.2 — 2026-07-30
- Mode `--digest` en ligne de commande (un envoi ponctuel, par exemple au démarrage de l'ordinateur).

## 0.1 — 2026-07-09
- Première version : un digest quotidien `@everyone` sur Discord des actualités de physique des particules et des ondes gravitationnelles.
