# Notes, théorie de la cause, documents, courriels et gabarits

Analyse probante et théorie : `analyse-et-strategie` ; rédaction d'actes : `redaction-juridique` ; bordereaux, cotes, cahiers, endos et conformité formelle : `production-et-conformite` ; jugement et appel : `phase-jugement-et-suites` (compétences locales en session ; aucun nom, n° dossier ni extrait de pièce vers le web, `lois.find_relevant` / `lois.search_text` ou `juris.*`).

## N1 Trouver, lire ou modifier une note
{{GEN:charger}}
- **Déclencheur** : « retrouve ma note sur … », « corrige la note ».
- **Appels** : (1) `list_notes(dossier_id, query=…)` ; dossier inconnu : `list_notes(scope="cabinet", query=…)` ; (2) `get_note(note_id)` sur la note retenue ; (3) `update_note(note_id, …, expected_etag)`, ou `append_to_note(note_id, content)` pour ajouter à la fin.
- **Arrêt** : l'aperçu de rangée suffit souvent pour identifier la note.
- **À éviter** : `list_notes` sans `dossier_id` ni `scope` (ne lit que Général).

## N2 Écrire la théorie de la cause
{{GEN:charger}}
- **Déclencheur** : « verse ce plan dans la théorie », « complète le bloc C ».
- **Appels** : (1) `get_note(note_id=analyse_note_id)` → `structure` et `etag` ; si `analyse_note_state` est "absent", confirmer puis `edit_analyse(dossier_id)` seul crée la théorie ; (2) retirer les titres de niveaux 1 et 2 du texte et découper par bloc ; (3) confirmer, puis `edit_analyse(dossier_id, operations=[…], expected_etag, idempotency_key)` en un appel.
- **Arrêt** : pas de relecture.
- **À éviter** : `list_notes` pour trouver la théorie (`analyse_note_id` est dans `get_dossier`).

## Doc1 Ce document est-il déjà versé ?
{{GEN:charger}}
- **Déclencheur** : « as-tu le jugement ? » ; avant tout téléversement.
- **Appels** : (1) `list_documents(dossier_id, category=…)` ou `query=…` ; dossier inconnu : `list_documents(scope="cabinet", query=…)`.
- **Arrêt** : `query` ne fouille que les métadonnées : une liste vide ne prouve pas l'absence de la pièce.
- **À éviter** : relire la liste du même dossier dans la même conversation.

## Doc2 Lire un document
{{GEN:charger}}
- **Déclencheur** : « que dit la pièce P-3 ».
- **Appels** : (1) `get_document_text(document_id)`, puis `page_range` (`next_page`) seulement si tronqué.
- **Arrêt** : citer le strict nécessaire ; signaler `has_text: false` honnêtement (aucune OCR).
- **À éviter** : lire tout le document si une page ou une date suffit.

## Doc3 Qualifier un lot
{{GEN:charger}}
- **Déclencheur** : « qualifie les documents non analysés du 2026-008 ».
- **Appels** : (1) `list_documents(dossier_id, limit=50)` puis `next_offset`, retenir `analysee: false` et PDF ou `.docx` ; (2) confirmer le lot ; (3) par document : `get_document_text` puis `record_document_analysis` (table fermée `sous_nature`), en parallèle avec clés distinctes.
- **Arrêt** : rapporter qualifiés (statut présumé), illisibles et restants.
- **À éviter** : réanalyser sans demande (journal append-only) ou qualifier sur le seul nom de fichier.

## Doc4 Verser un fichier de l'extérieur
{{GEN:charger}}
- **Déclencheur** : « verse ce PDF au dossier ».
- **Appels** : (1) Doc1 d'abord ; (2) `begin_upload(purpose="document", …)`, PUT des octets à `upload_url` depuis le bac à sable, puis `finalize_upload(ticket_id)`.
- **Arrêt** : sans bac à sable capable du PUT vers `storage.googleapis.com`, renvoyer à l'application.
- **À éviter** : ouvrir un second ticket pour un fichier déjà déposé.

## Doc5 Générer, gabariser, classer
{{GEN:charger}}
- **Déclencheur** : « prépare la lettre sur gabarit », « classe ces pièces ».
- **Appels** : (1) `list_templates(kind="gabarit", query=…)`, puis `list_templates(template_id, dossier_id)` : blocs et champs requis ; (2) confirmer, puis `fill_gabarit(template_id, dossier_id, blocs, champs_manuels, idempotency_key)` ; (3) Markdown sur gabarit d'impression : `create_document(source="markdown", dossier_id, title, markdown)` ; (4) gabariser : `preview_templatize(document_id, substitutions)`, puis `create_template(source_document_id, name, category, substitutions)` (`index` base 0, « n° 1 » base 1) ; (5) classer : `move_documents` (50 max), `manage_folder`, `update_document`.
- **Arrêt** : rapporter document créé et `folder` (souvent « Interne › Projets »).
- **À éviter** : `list_templates(dossier_id)` sans `template_id` (refusé) ou classer pièce par pièce via `update_document`.

## Doc6 Trier et classer les courriels
{{GEN:charger}}
- **Déclencheur** : « trie ma boîte de réception », « classe ces courriels ».
- **Appels** : (1) `get_courrier_connexion()` (`pret` / `motifs`), puis `list_courriels(premier=0, nombre=25, filtre="nouveaux")` (et `premier` suivant tant que `suite` vaut `true` ; aussi `provenance="envoyes"` avec `depuis=…`) ; (2) rattacher chaque courriel via `list_dossiers(query=…)` ou `get_dossier` et vérifier les pièces des envois via Doc1 ; (3) confirmer (sauf tâche planifiée autorisée), puis dans l'ordre `save_courriels(messages=[…], idempotency_key)` (OneDrive et Athena, catégorie présumée ; pièces nommées d'office sans `nom`, `chemin` retourné), `move_courriels(messages=[…], idempotency_key)` (`action="classer"` ou `action="laisser"` si incertain) et `create_time_entries_bulk` au besoin.
- **Arrêt** : laisser en place avec `action="laisser"` tout courriel ambigu ; signaler tout type non pris en charge dans Athena (sauvé dans OneDrive).
- **À éviter** : appeler `move_courriels` (`action="classer"`) avant `save_courriels`, ou sauvegarder une pièce d'envoi déjà présente selon Doc1.
