# Facturation et budget : encours, période, facture, phases, budget

Tout ajout ou reclassement passe par les outils `*_bulk` (de **1 à 50** lignes par appel).

## F1 Non facturé, encours
{{GEN:charger}}
- **Déclencheur** : « combien de non facturé », « quels dossiers », « qui me doit ».
- **Appels** : (1) cabinet : `get_billing_snapshot()` (`by_dossier` pour la ventilation, `outstanding_invoices` pour les factures impayées) ; (2) un dossier : `get_billing_snapshot(dossier_id)` pour le non facturé ; solde dû : `list_invoices(dossier_id, status_group="impayée")` et additionner `balance_cents`.
- **Au besoin** : si `outstanding_invoices_truncated` est vrai : `list_invoices(status_group="impayée")` toutes pages ; si `by_dossier_truncated` est vrai : avertir que la ventilation est partielle.
- **Arrêt** : citer `outstanding_display` (cabinet), jamais `total_outstanding` (inclut brouillons) ni additionner `amount_due` ; `payment_basis` "none" = aucun paiement inscrit au registre.
- **À éviter** : appeler `list_time_entries` dossier par dossier pour calculer l'encours.

## F2 Travail d'une période
{{GEN:charger}}
- **Déclencheur** : « qu'ai-je fait en septembre », « mes heures de la semaine ».
- **Appels** : (1) `list_time_entries(date_from=…, date_to=…, limit=50)` sans `dossier_id` (sauf dossier nommé) ; (2) même tour si demandé : `list_expenses(date_from=…, date_to=…, limit=50)` ; (3) suivre `next_cursor`.
- **Arrêt** : totaliser les rangées lues ; si `truncated` est vrai, prévenir ou finir la pagination.
- **À éviter** : boucler `list_time_entries` sur chaque dossier.

## F3 Facture
{{GEN:charger}}
- **Déclencheur** : « prépare la facture du 2026-022 », « facture le non facturé ».
- **Appels** : (1) `preview_invoice(dossier_id, all_unbilled=true)` (ou `time_entry_ids` / `expense_ids`) ; si `ready` vaut faux, s'arrêter ; (2) montrer lignes, taxes, total et TOUJOURS confirmer ; (3) `create_invoice` avec même sélection et `expected_total_cents`.
- **Au besoin**, sur demande : `create_document(source="invoice_note", invoice_id)`.
- **Arrêt** : rapporter le numéro (consommé pour toujours) ; la facture est en brouillon ; marquer `envoyée` ou annuler via `update_invoice` exige aussi confirmation expresse.
- **À éviter** : appeler `get_dossier` ou `get_billing_snapshot` avant `preview_invoice`, ou `list_invoices` après.

## F4 Reclasser les phases
{{GEN:charger}}
- **Déclencheur** : « reclasse les heures sans phase du 2026-003 ».
- **Appels** : (1) `list_time_entries(dossier_id, limit=50)` jusqu'au bout (`next_cursor`), filtrer `phase` vide ; (2) libellés : un `get_reference_vocabulary(kind="phases")` par conversation ; tableau des codes, confirmer ; (3) `set_time_entry_phase_bulk` (1 à 50 lignes par appel).
- **Au besoin**, déboursés : `list_expenses(dossier_id, limit=50)` au même tour, puis `set_expense_phase_bulk` (1 à 50 lignes).
- **Arrêt** : rapporter appliquées, inchangées, refusées (valide même sur ligne facturée).
- **À éviter** : relire `get_reference_vocabulary` par rangée ou corriger une entrée facturée via `update_time_entry`.

## F5 Budget
{{GEN:charger}}
- **Déclencheur** : « où en est le budget », « révise le budget ».
- **Appels** : (1) `get_budget(dossier_id, include_history=false)` (historique sur demande) ; (2) réviser : tableau des lignes, confirmer, puis `create_budget_version(dossier_id, base_version, mode, lines)`.
- **Arrêt** : `get_budget` répond à l'état d'avancement (réalisé inclus).
- **À éviter** : ajouter `get_billing_snapshot` ou `list_time_entries`.
