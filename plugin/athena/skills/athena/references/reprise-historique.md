# Reprise de données historiques, un dossier à la fois

## Reprise d'un dossier
{{GEN:charger}}
- **Déclencheur** : « reprends ce fonds ».
- **Appels** : (1) `find_imported(legacy_ref)` par contact, dossier et facture, en parallèle ; (2) manquants : `create_partie`, `create_dossier`, avec `legacy_ref` ; (3) sources (tableau confirmé) : `create_time_entries_bulk`, `create_expenses_bulk` (tranches de 1 à 50, une `legacy_ref` par rangée ; retirer ce que `find_imported` trouve déjà) ; (4) TOUJOURS confirmer, puis `import_invoice(dossier_id, invoice_number, date, expected_total_cents, time_entry_ids, expense_ids)` depuis `entities` ; (5) `get_import_audit(dossier_id)`.
- **Arrêt** : la facture reste brouillon ; `update_invoice` → `envoyée` sur mot exprès de l'avocat seulement.
- **À éviter** : créer une ligne par appel au lieu de grouper jusqu'à 50 lignes dans `create_time_entries_bulk` et `create_expenses_bulk`, ou relister `list_time_entries` par dossier.

Plafonds qui diffèrent entre outils :
{{GEN:limites_reprise}}
