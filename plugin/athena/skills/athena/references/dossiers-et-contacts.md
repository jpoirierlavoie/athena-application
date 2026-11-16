# Dossiers et contacts : trouver, créer, corriger, conformité, registres

`find_imported` et `legacy_ref` : en reprise historique seulement. Conformité déontologique et délais substantiels : `deontologie-et-pratique` et `administration-litige` (sans exposer d'identité client).

## D1 Trouver un contact, ou ses dossiers
{{GEN:charger}}
- **Déclencheur** : « trouve le contact », « ses dossiers ? ».
- **Appels** : (1) `list_parties(query=…)`, un mot, si le dossier lu n'a pas l'id ; (2) `get_partie(partie_id)` pour les coordonnées complètes ou `dossiers[]`.
- **Arrêt** : `dossiers[]` répond à « quels dossiers ».
- **À éviter** : rafales de `list_parties` avant d'avoir lu le dossier.

## D2 Nouveau contact
{{GEN:charger}}
- **Déclencheur** : « crée la fiche de … ».
- **Appels** : (1) doublon : `list_parties(query=…)`, un mot par appel (si vide, second mot) ; (2) confirmer ; (3) `create_partie(type, …, idempotency_key)`.
- **Arrêt** : rapporter l'id.
- **À éviter** : créer sans vérifier l'existant : un contact ne se supprime pas.

## D3 Nouveau dossier
{{GEN:charger}}
- **Déclencheur** : « ouvre un dossier, Béton Nord c. X ».
- **Appels** : (1) demander le n° s'il manque (`create_dossier` refuse un n° pris) et D1 pour les parties ; (2) D2 pour les manquantes ; (3) `get_reference_vocabulary(kind="actions", domaine=…)` (recouvrement : REC ; `"domaines"`, `"forums"`, `"districts"` si code inconnu seulement) ; (4) confirmer, puis `create_dossier(file_number, title, clients, …)`.
- **Arrêt** : rapporter l'id, le n° et les `warnings` (sans arborescence, le dossier existe : ne pas le recréer) ; `get_dossier` seulement si la fiche complète est demandée.
- **À éviter** : charger tous les vocabulaires d'avance.

## D4 Corriger un dossier ou un contact
{{GEN:charger}}
- **Déclencheur** : « corrige le titre », « ferme le dossier ».
- **Appels** : (1) champ vide : `complete_dossier` ; remplacer : `update_dossier` (champs modifiés seulement, `expected_etag`) ; (2) ajouter une partie : `update_dossier(add_clients=[…])` ou `add_opposing_parties` ; rôles, avocat, détachement : `update_dossier_party(action=…)` ; (3) statut (TOUJOURS confirmer) : `set_dossier_status`, relayer les `warnings` ; (4) contact : `update_partie` (bloc adresse complet sur 6 clés ; nom figé répercuté par `update_dossier_party(action="refresh_names", partie_id=…)`) ; mandataire : `update_partie_mandataire`.
- **Arrêt** : pas de relecture hors `warnings`.
- **À éviter** : relire le dossier avant et après chaque correction.

## D5 Conformité : identité, conflits
{{GEN:charger}}
- **Déclencheur** : « vérifie les conflits », « vérifie KYC ».
- **Appels** : (1) actifs : `get_coverage_report(checks=["CONFLIT_NON_VERIFIE", "IDENTITE_NON_VERIFIEE"])` ; un client : `get_partie(partie_id)` ; (2) TOUJOURS confirmer, puis `record_kyc_status(partie_id, check, status, notes, expected_etag)`.
- **Arrêt** : dire « inscrit, présumé — à confirmer à l'application », jamais « vérifié » ni « identifié » ; règles déontologiques : `deontologie-et-pratique`.
- **À éviter** : appeler `get_partie` par client pour un balayage global.

## D6 Signification, événement de prescription
{{GEN:charger}}
- **Déclencheur** : « signifié le … », « la demande est déposée ».
- **Appels** : (1) depuis le dossier lu (sinon `get_dossier`) : `partie_id` et `significations[]` ; (2) confirmer, puis `record_signification(dossier_id, partie_id, date, mode)` (un par partie, en parallèle) ; (3) `record_prescription_event(dossier_id, type, date)`.
- **Au besoin** : délai en jours résultant → `compute_judicial_deadline(start_date, delay_days, direction)` puis `create_task`.
- **Arrêt** : rapporter l'échéance ou l'alerte mise à jour ; délais en mois / droit substantiel → `administration-litige` (sans données nominatives).
- **À éviter** : réinscrire pour « corriger » : ces registres sont append-only.
