# Comptabilité : fidéicommis et administration, soldes et écritures

## Soldes et registres : fidéicommis, administration
{{GEN:charger}}
- **Déclencheur** : « combien en fidéicommis », « solde du client », « chèques en circulation », « conciliation en retard », « solde du compte d'opérations », « états financiers », « comparatif ».
- **Appels** : (1) cabinet : `get_trust_snapshot()` (comptes, conciliations, chèques en circulation, `by_dossier`) ; (2) un dossier par client : `get_trust_balance(dossier_id)` ; (3) mouvements : `list_trust_transactions(dossier_id, client_id)` (carte client) ou `list_trust_transactions(account_id)` (journal paginé par `cursor` ; ailleurs, `truncated` → resserrer `date_from`/`date_to`) ; (4) compte d'opérations et carte : `get_admin_ledger()` (solde cumulatif avec `account_id` seul, sans `kind`, `status`, `category`) ; (5) états financiers : `get_financial_statements(periods)`.
- **Arrêt** : `book_*` = « Solde aux livres » ; `cleared_*` = « Disponible (compensé) » (jamais appelé « Solde ») ; écart = en transit.
- **À éviter** : boucler `get_trust_balance` par dossier pour un portrait global.

## Pièces justificatives : consulter, joindre, remplacer
{{GEN:charger}}
- **Déclencheur** : « joins le reçu », « pièce justificative », « remplace la facture du fournisseur », « que dit ce reçu ».
- **Appels** : (1) écriture : `get_admin_ledger()` (`id` et `receipt`) ; (2) consulter : `get_admin_receipt(tx_id)` ; (3) joindre : `begin_upload(purpose="piece_justificative", tx_id, …)`, PUT à `upload_url`, puis `finalize_upload(ticket_id)` ; remplacer : TOUJOURS confirmer avec `replaces_receipt_md5` = `md5_base64` lu.
- **Arrêt** : l'ancienne pièce reste conservée ; une dépense de dossier en reçoit copie dans « Mandat › Déboursés ».
- **À éviter** : décrire une image non lue via `get_admin_receipt` ; chercher un reçu cabinet dans les documents d'un dossier.

## Écrire aux registres : fidéicommis, administration
{{GEN:outils_comptables}}
- **Déclencheur** : « inscris le dépôt », « paie la facture depuis le fidéicommis », « le client a payé la facture », « inscris la dépense du cabinet », « crédits Claude ou infonuagique », « je me suis versé », « virement au fidéicommis », « l'avance d'un client est arrivée au compte d'opérations », « un fournisseur m'a remboursé », « remise ou remboursement de TPS/TVQ », « compense ces chèques ».
- **Appels** : (1) lire la description de l'outil visé avant son premier appel ; (2) n'inscrire qu'un mouvement bancaire RÉEL survenu à sa date, TOUJOURS confirmé, avec `idempotency_key`.
- **Arrêt** : aucune suppression ; fidéicommis = contre-passation seule (`reverse_register_entry`) ; administration = `update_admin_entry` tant qu'éditable, sinon contre-passation. Conciliation, virement entre dossiers, compte bancaire = application seule.
- **À éviter** : inscrire un mouvement bancaire en note/tâche ; confondre `prélèvement` (retrait avocat) avec le mode préautorisé « prélèvement » ; passer un paiement carte en virement interne ; mettre `remise_tps_tvq` en dépense/`taxes_permis`/fidéicommis (sans dossier ; intérêts/pénalités à part) ou `remboursement_tps_tvq` en `recette_autre` (demander l'objet de tout autre paiement à Revenu Québec) ; classer IA/cloud en `abonnements` (utiliser `infonuagique_ia`), un ouvrage en `abonnements` (`documentation`), SOQUIJ/registres en `abonnements` (`recherche`), timbres/greffe en `taxes_permis` (`timbres_judiciaires`), notaire/expert/avocat en `honoraires_professionnels` (clé dédiée), salle ponctuelle en `loyer` (`location_salle`) ; mettre une avance reçue aux opérations en `recette_autre` (`avance_honoraires` puis virement `transfer_purpose` « avances ») ou un remboursement fournisseur en `recette_autre` (`remboursement_depense`, catégorie et ventilation d'origine).
