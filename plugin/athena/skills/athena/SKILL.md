---
name: athena
description: "Guide d'utilisation du connecteur Athena (pratique de litige civil québécois) : séquences d'appels courtes, résolution d'identifiants et garde-fous d'écriture. Activer dès qu'une demande lit ou écrit dans Athena, même sans le nommer (Quebec litigation case & matter management, billing, trust accounting, deadlines, docs). Déclencheurs : breffage, qu'est-ce qui s'en vient, dossier, client, parties, conflits, KYC, tâche, échéance, audience, Bookings, protocole, note, théorie de la cause, document, gabarit, heures, débours, non facturé, facture, budget, fidéicommis, administration, états financiers, reprise. Complète droit-civil-quebecois (sans lui exposer de données nominatives). Ne pas utiliser pour trancher le droit, rédiger un acte ni qualifier hors Athena."
---

{{GEN:version}}

<!-- SAFETY-KERNEL:START -->
## Noyau

- **Autorité** : `NOYAU DE SÉCURITÉ` (`INSTRUCTIONS`) et descriptions des outils priment ; ici, routage. Outils sans préfixe.
- **Confirmation graduée** :
  - *Lecture* : aucune.
  - *Écriture courante* : tableau et confirmation avant écriture (demander dossier, montant, texte ou numéro manquants), sauf instruction permanente de l'avocat.
  - *Écriture à fort impact* (TOUJOURS confirmer même sous instruction permanente) : `set_dossier_status`, `record_kyc_status`, `update_dossier_party(action="remove")`, `decide_rendez_vous`, `create_invoice`, tout statut `update_invoice`, toute écriture comptable, toute pièce justificative.
- **Clé et concurrence** : `<outil>-<n° dossier|general|lot>-<AAAAMMJJ>-<étiquette>-<n>` (4 lettres fixes par conversation). `idempotent_replay: true` au 1er essai = déjà inscrit. `expected_etag` = dernier `etag` lu. « … n'a pas pu être lu » = panne, jamais absence : réessayer sans recréer. `dry_run` refusé. `edit_analyse(dossier_id)` sans `operations` ni `full` ÉCRIT (crée la théorie).
- **Cloisonnement (`secret professionnel`)** : aucun nom de partie, n° dossier, adresse, pièce ni chiffre bancaire vers le web ou `droit-civil-quebecois` (`recherche-juridique`, `redaction-juridique`, `analyse-juridique`, `administration-litige`, `deontologie-et-pratique`) :
  - `lois.*`, `juris.*`, `recherche-juridique` : droit abstrait seulement.
  - `redaction-juridique`, `analyse-juridique` : rôles anonymisés (« Demandeur », « Défendeur »), faits caviardés.
  - `outils.calendrier_deadlines`, `outils.interets_1619` : dates et montants neutres sans dossier.
  - `outils.registre_search` / `outils.registre_get_entity` : registre public sur demande expresse.

## Conventions hors de la tête

- `contingency_percent` : lu en % (`get_dossier` : 25.0), écrit en points de base (2500).
- Date seule : aucun fuseau ; « aujourd'hui » = Montréal.
<!-- SAFETY-KERNEL:END -->

## L'application en une minute

- Pivot : le dossier ; « Général » = notes, tâches, événements hors dossier ; une théorie par dossier.
- Contacts au cabinet ; le dossier fige leurs noms pour gabarits/procédures.
- Téléphone : les contacts sauf les fournisseurs ; tâches, notes, événements des dossiers actifs/`en_attente` et Général (jamais finances, documents, ni protocoles hors tâche liée).
- Droit : `droit-civil-quebecois` ; courriels : Microsoft 365 (envoi uniquement sur demande).

## Économie d'appels

1. **Un `get_dossier` par dossier et conversation.** Le résultat d'une écriture vaut relecture (`etag`, `dossier_etag`, `warnings`) ; relire seulement si incertain, `ENREGISTRÉE — NE PAS RÉESSAYER` ou `stale_etag`.
2. **Rangées avec `etag`** : éditer tâche, événement ou étape depuis sa rangée.
3. **Partir du cabinet** : `get_agenda`, `get_billing_snapshot()`, `get_coverage_report`, `list_time_entries(date_from=…)` sans dossier.
4. **`limit=50`** pour un lot borné voulu en entier.
5. **Appels indépendants en parallèle** dans un tour.
6. **`get_reference_vocabulary`** : une fois par `kind` ("domaines", "actions", "forums", "districts", "prescription_types", "phases").
7. **Ne pas recalculer** `is_overdue`, statut d'étape, `last_action_date`, `next_deadline_date`.
8. **Claude Code** : charger **Charger** via ToolSearch `select:` ; **Au besoin** seulement si requis.
9. **S'arrêter à la réponse.** Note = livrable, jamais brouillon temporaire.

## D'où vient chaque identifiant

- **dossier_id** : `list_dossiers(query=…)` (n° ou mot du titre) ; `get_partie` → `dossiers[]` ; n° exact → `get_dossier(file_number)`.
- **partie_id** : `clients`/`opposing_parties` de `get_dossier` ; sinon `list_parties(query=…)` (un mot ; réessayer sans accents).
- **note_id** : `list_notes` ; théorie : `analyse_note_id` de `get_dossier`.
- **Tâche, événement, étape** : rangées ; **document/répertoire** : `list_documents(dossier_id, include_folders=true)` ; **gabarit** : `list_templates` ; **facture** : `list_invoices`.

## Routage et références

Lire au plus **une** feuille ci-dessous avant d'agir hors R1–R4 ; `references/index-outils.md` si aucune recette ne convient :

- Contacts, dossiers, KYC/conflits, significations/prescription → `references/dossiers-et-contacts.md`
- Tâches, audiences, Bookings, échéances, protocole → `references/agenda-et-protocole.md`
- Notes, théorie de la cause, documents, téléversement, gabarits → `references/notes-et-documents.md`
- Non facturé, période, facture, reclassement phases, budget → `references/facturation-et-budget.md`
- Fidéicommis, grand livre d'administration, reçus, états financiers → `references/comptabilite-et-etats.md`
- Reprise historique d'un dossier → `references/reprise-historique.md`
- Lectures directes sans feuille : `list_documents(dossier_id)`, `get_document_text(document_id)`, `list_notes(dossier_id)`/`list_notes(scope="cabinet")`, `list_protocol_steps(dossier_id)`, `compute_judicial_deadline`.

### R1 Breffage
{{GEN:charger}}
- **Déclencheur** : « qu'est-ce qui s'en vient ».
- **Appels** : (1) `get_agenda(days_ahead=7)` — il se suffit.
- **Au besoin**, si demandé, même tour : `list_hearings(bookings="pending")`, `get_trust_snapshot()`, `get_coverage_report()`.
- **Arrêt** : répondre de ces résultats sans descendre par dossier.
- **À éviter** : enchaîner `get_dossier` et `list_tasks` par élément d'agenda.

### R2 Heures et débours
{{GEN:charger}}
- **Déclencheur** : « inscris mes heures », « note ces déboursés ».
- **Appels** : (1) ids manquants : `list_dossiers(query=…, limit=5)` en parallèle ; (2) tableau confirmé (dossier, date, heures ou montant, texte facturé) ; (3) `create_time_entries_bulk` et/ou `create_expenses_bulk` (même pour 1 ligne, tranches de 50 ; taux du dossier sauf précision, phase seulement si demandée).
- **Arrêt** : rapporter les montants renvoyés, sans relister.
- **À éviter** : multiplier des appels d'une seule ligne ou relire `list_time_entries` par dossier.

### R3 Note au dossier
{{GEN:charger}}
- **Déclencheur** : « note au dossier », un résumé.
- **Appels** : (1) `get_dossier` s'il n'est pas déjà lu ; (2) note entière d'un jet (max 20 000 car.) ; (3) confirmer, `create_note(dossier_id, title, content, category, idempotency_key)`.
- **Arrêt** : rapporter l'id ; ni `list_notes` avant, ni `append_to_note` après.
- **À éviter** : créer puis ajouter aussitôt par `append_to_note`.

### R4 Contexte de dossier
{{GEN:charger}}
- **Déclencheur** : portrait d'un dossier, parties, théorie, greffe.
- **Appels** : (1) `get_dossier` : fiche, parties, greffe, `significations[]` (`pv_document_id`), prescription, `analyse_note_id` ; (2) théorie : `get_note(note_id=analyse_note_id)`, sinon dire l'`analyse_note_state`.
- **Au besoin** : `get_partie` (désignations), `get_document_text(document_id=pv_document_id)`.
- **Arrêt** : `found: false` sur un `file_number` fait foi.
- **À éviter** : `list_parties` pour une partie du dossier, `list_notes` pour la théorie ou `parse_court_file_number` sur le n° lu.

## Ce que les champs ne valent pas

`role` dérive des parties ; catégorie, analyse et KYC (`record_kyc_status`) restent présumés ; échéance CS reste indicative. Signaler sans corriger d'office.

## Seule l'application

Le connecteur ne peut jamais non plus :
{{GEN:seule_application}}
Renvoyer à l'écran correspondant ; un Bookings confirmé se reporte ou s'annule dans Outlook.
