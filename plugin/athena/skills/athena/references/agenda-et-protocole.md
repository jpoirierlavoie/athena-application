# Agenda et protocole : tâches, audiences, Bookings, échéances, protocole

Délais en mois, calendrier d'instance C.p.c. et règles de computation complexes : `administration-litige` (dates neutres seulement, sans identifiants client).

## A1 Tâches
{{GEN:charger}}
- **Déclencheur** : « ajoute une tâche », « c'est fait ».
- **Appels** : (1) confirmer, puis `create_task(dossier_id, title, due_date)`, une clé chacune ; (2) fermer : `complete_task(task_id)`, `warnings` relayés.
- **Au besoin** : rouvrir, `reopen_task` ; modifier, `update_task` avec l'`etag` de la rangée ; doute sur un doublon : `list_tasks(dossier_id, include_completed=true)`, par dossier seulement.
- **Arrêt** : pas de relecture.
- **À éviter** : `list_tasks(include_completed=true)` sans `dossier_id` (retourne les closes les plus anciennes).

## A2 Audiences et rendez-vous
{{GEN:charger}}
- **Déclencheur** : « inscris l'audience du … », « chaque lundi à 9 h ».
- **Appels** : (1) confirmer, puis `create_hearing(dossier_id, title, date, start_time, hearing_type)`, avec `status="confirmée"` si la date est certaine ; (2) série récurrente : `create_hearing_series`.
- **Au besoin** : modifier, `list_hearings(dossier_id)` (`date_from` pour le passé), puis `update_hearing(hearing_id, …, expected_etag)`.
- **Arrêt** : pas de relecture ; la synchronisation Outlook suit seule : ne pas créer en double dans Microsoft 365.
- **À éviter** : appeler `create_hearing` par occurrence d'une série.

## A3 Demandes Bookings
{{GEN:charger}}
- **Déclencheur** : « des rendez-vous à confirmer ? ».
- **Appels** : (1) `list_hearings(bookings="pending")` ; (2) accord EXPRÈS de l'avocat pour chaque demande, puis `decide_rendez_vous(hearing_id, action, expected_etag, idempotency_key)`.
- **Arrêt** : rappeler avant accord que `action="refuser"` annule la réunion Outlook et avise le client ; rapporter `client_notified`.
- **À éviter** : chercher une demande en attente dans la fenêtre de dates ordinaire.

## A4 Échéances
{{GEN:charger}}
- **Déclencheur** : « signifié le …, 15 jours : inscris l'échéance ».
- **Appels** : (1) ancrage connu : `compute_judicial_deadline(start_date, delay_days, direction)` en jours (plusieurs par tour) ; (2) confirmer, puis `create_task(dossier_id, title, due_date, description)` avec la base du calcul dans `description`.
- **Au besoin** : une échéance déjà inscrite se lit (`get_agenda`, `list_protocol_steps(dossier_id)`, `next_deadline_date`) sans la recalculer.
- **Arrêt** : rapporter la date et la tâche ; délais en mois / computation C.p.c. → `administration-litige`.
- **À éviter** : recalculer systématiquement chaque échéance de l'agenda.

## A5 Protocole de l'instance
{{GEN:charger}}
- **Déclencheur** : « crée le protocole », « ajoute une étape ».
- **Appels** : (1) `list_protocol_steps(dossier_id)` : le statut dérivé gouverne ; (2) CQ ou CS : un seul `create_protocol(dossier_id, protocol_type, start_date, create_linked_tasks=true)` confirmé (`false` sans tâches) ; (3) étape sur mesure : `add_protocol_step(protocol_id, title, deadline_date, create_linked_task=true)` ; modifier : `update_protocol_step(protocol_id, step_id, …, expected_etag)`.
- **Arrêt** : pas de relecture.
- **À éviter** : un `create_task` par étape de protocole ; `list_protocol_steps` par item d'un breffage.
