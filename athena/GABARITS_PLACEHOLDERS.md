<!--
For developers. This French guide is the page the Gabarits list opens
(/gabarits/aide, routes/doc_templates.py) AND the contract that
tests/test_gabarits_placeholders_doc.py pins against the code:
utils/docx_fill.py (syntax), utils/template_fields.py (catalogue, flat
aliases, manual and passthrough fields), utils/invoice_docx.py (note
d'honoraires and credit note), utils/note_docx.py and utils/markdown_docx.py (note print),
utils/docx_templatize.py (templatizing). Add or rename a field there: update
this guide. Write it ONE line per paragraph and per list item — the page's
Markdown pipeline prints every line break (tests/test_gabarits_guide.py).
-->

# Champs des gabarits — guide de référence

Tous les champs que vous pouvez placer dans un **gabarit** Word (`.docx`) — qu'il serve à produire un document, une **note d'honoraires**, une **note de crédit** ou l'**impression d'une note** — et les règles de syntaxe qui les régissent.

> **Ce qui fait foi.** Ce guide recense ce que l'application remplit réellement. En cas d'écart, c'est le code de l'application qui l'emporte; à chaque mise à jour de l'application, un test automatique vérifie que ce guide nomme bien tous les champs, alias courts et choix qu'elle connaît.

---

## 1. Syntaxe — les formes de champ

| Forme | Effet | Où l'employer |
|---|---|---|
| `{{name}}` | **Champ simple** (`name` : le nom du champ) — remplacé par sa valeur. | corps du document, en-têtes et pieds de page |
| `{{#region}}` | **Rangée répétée** (`region` : le nom de la rangée, par exemple `ligne_honoraire`) — placé dans la *première cellule* d'une rangée de tableau, le marqueur fait reproduire cette rangée (la plus intérieure, si des tableaux sont imbriqués) une fois par élément. **Aucun marqueur de fermeture** : la répétition s'arrête à la limite de la rangée. Une liste vide supprime la rangée marquée. | note d'honoraires (corps du document seulement) |
| `{{?cond}}` … `{{/cond}}` | **Section conditionnelle** (`cond` : le nom de la condition, par exemple `si_honoraires`) — placez chacun des deux marqueurs *dans son propre paragraphe*, l'un avant un tableau, l'autre après. Si la condition est fausse, tout l'ensemble (marqueurs et tableau) est supprimé; si elle est vraie, seuls les paragraphes des marqueurs sont retirés. Un marqueur d'ouverture sans marqueur de fermeture, ou l'inverse, fait échouer la génération. | note d'honoraires (corps du document seulement) |

### Règles à surveiller

- **Caractères permis dans un nom :** lettres (accentuées comprises, `À–ÿ`), chiffres `0–9`, trait de soulignement `_` et point `.` — **pas d'espace dans le nom, pas de trait d'union**. Vous pouvez mettre des espaces *autour* du nom : `{{ name }}` équivaut à `{{name}}`.
- **Le nom est reconnu sans égard aux majuscules**, pour les champs automatiques comme pour les champs à saisir de la section 4 : `{{tribunal}}`, `{{Tribunal}}` et `{{TRIBUNAL}}` désignent le même champ, tout comme `{{privilège}}` et `{{PRIVILÈGE}}`. **Les accents, eux, comptent** : `{{privilege}}`, sans accent, est un nom inconnu, laissé tel quel.
- **Un nom tout en majuscules met la valeur en majuscules**, pour les champs automatiques comme pour les champs à saisir : `{{TRIBUNAL}}` → `COUR SUPÉRIEURE`, `{{TRANSMISSION_LETTRE}}` → `COURRIEL`, alors que `{{transmission_lettre}}` → `courriel`. Une même liste de choix sert donc autant pour une mention en majuscules en tête de lettre que pour une phrase du texte.
- **Un nom inconnu est laissé tel quel.** Un champ dont le nom n'est pas connu demeure `{{name}}`, tel quel, dans le document produit, pour que vous le complétiez dans Word; il ne fait jamais échouer la génération (voir la section 5).
- **Une valeur de plusieurs paragraphes se répartit d'elle-même.** Une valeur qui contient une ligne vide est répartie en plusieurs paragraphes, chacun copié sur le paragraphe où se trouve le champ (la numérotation d'une liste se poursuit). Dans la fenêtre de génération, un tel champ s'affiche dans une **zone de texte** de plusieurs lignes : une case d'une seule ligne supprimerait les sauts de ligne, et la valeur ne serait jamais répartie, sans le moindre avertissement. (`{{dossier.sommaire}}` et les champs `_avec_adresse` des parties reposent sur ce mécanisme.)
- **Chaque valeur a un plafond; au-delà, la génération est REFUSÉE — jamais tronquée** : 2 000 caractères pour un champ à saisir; 5 000 pour un champ automatique (soit la longueur permise du plus long champ enregistré, `dossier.sommaire`), ou la longueur de la valeur que l'application compose elle-même lorsqu'elle est plus longue; 20 000 pour une valeur de plusieurs paragraphes. Dans la fenêtre de génération, la case de saisie vous arrête au même nombre.
- **Les choix faits dans la fenêtre de génération sont vérifiés au moment de générer.** Si un client ou une partie adverse n'est plus au dossier, ou si un destinataire n'existe plus, la génération est refusée avec un message : jamais la première partie du dossier n'est prise à sa place.
- **Valeur manquante → marqueur visible.** Un champ automatique sans valeur s'imprime `[CHAMP MANQUANT : name]`; un champ à saisir laissé vide s'imprime `[À COMPLÉTER : name]`. Les noms laissés tels quels ne reçoivent ni l'un ni l'autre marqueur : `{{name}}` reste tel que vous l'avez tapé.
- **Champ « fragmenté ».** Il arrive que Word découpe un champ tapé en plusieurs segments dans la structure interne du fichier (le plus souvent au point de `{{dossier.defendeur}}`). L'application recolle d'elle-même la plupart de ces segments; une coupure vraiment structurelle (un saut de ligne, une tabulation, une image, un code de champ ou un signet *entre* les accolades) est signalée par un avertissement au téléversement, et ce champ reste tel quel, `{{…}}`, dans le document produit, jusqu'à ce que vous le retapiez d'un seul trait dans Word.

---

## 2. Champs du dossier (remplis automatiquement)

Ces champs se remplissent automatiquement à partir du dossier et des parties choisies.

### `dossier.*`

| Champ | Valeur |
|---|---|
| `{{dossier.titre}}` | Titre du dossier |
| `{{dossier.sommaire}}` | Sommaire libre du dossier (la carte « Sommaire » de la page du dossier). Sur plusieurs paragraphes : chaque passage séparé par une ligne vide devient un paragraphe, copie de celui qui contient le champ; un simple saut de ligne devient une espace |
| `{{dossier.numero_cour}}` | Numéro de dossier judiciaire (« Préjudiciaire » tant que le forum du dossier est préjudiciaire, c'est-à-dire tant qu'aucune procédure n'a été déposée) |
| `{{dossier.reference_interne}}` | Référence interne (le champ « Notre référence » du dossier) |
| `{{dossier.tribunal}}` | Tribunal |
| `{{dossier.chambre}}` | Chambre / compétence |
| `{{dossier.district}}` | District judiciaire |
| `{{dossier.palais}}` | Palais de justice |
| `{{dossier.role}}` | Rôle du client dans le litige, tel qu'il est enregistré (p. ex. `demandeur`) |
| `{{dossier.role_feminin}}` | Rôle au féminin (demanderesse, défenderesse, …; `autre` → sans valeur) |
| `{{dossier.role_label}}` | Libellé du rôle, avec majuscule initiale (Demandeur, Défendeur, …) |
| `{{dossier.demandeur}}` | Nom du ou des demandeurs, **sans civilité** — alias de `{{dossier.demandeurs}}` (ci-dessous) |
| `{{dossier.defendeur}}` | Nom du ou des défendeurs, **sans civilité** — alias de `{{dossier.defendeurs}}` (ci-dessous) |
| `{{dossier.demandeur_avec_civilite}}` | Nom du ou des demandeurs, **avec** civilité |
| `{{dossier.defendeur_avec_civilite}}` | Nom du ou des défendeurs, **avec** civilité |
| `{{dossier.adresse_demandeur}}` | Adresse sur une ligne de la partie demanderesse |
| `{{dossier.adresse_defendeur}}` | Adresse sur une ligne de la partie défenderesse |
| `{{dossier.domaine}}` | Libellé du domaine, soit la famille d'actions de la taxonomie (« Recouvrement de créances », …) |
| `{{dossier.action}}` | L'action telle qu'on la cite : « Libellé [CODE] » (« Action sur compte [REC-01] ») |
| `{{dossier.action_libelle}}` | Le libellé de l'action seul, sans le code entre crochets |
| `{{dossier.action_code}}` | Le code seul (« REC-01 ») |
| `{{dossier.precision}}` | Précision libre sur l'action — obligatoire pour les actions « Autre (préciser) » (`-99`); contient aussi l'ancien texte « Objet » des dossiers créés avant la taxonomie |
| `{{dossier.delai}}` | Le délai **indicatif** que la taxonomie prévoit pour l'action, en forme courte (« 3 ans ») |
| `{{dossier.point_depart}}` | Le point de départ du délai de l'action et ses pièges (« Exigibilité de chaque facture ») |
| `{{dossier.reference}}` | La source législative du **délai** (« Arts. 2925 et 2931, C.c.Q. »). Six actions n'ont aucune source législative de délai : pour elles, le champ reste sans valeur (CST-05, COR-04, COR-09, FAM-01, FAM-02, FAM-06) |
| `{{dossier.fondement}}` | Le fondement législatif du droit d'action (Annexe C de la taxonomie v1.2; « C.c.Q. » sous-entendu : « 1590; 1708, 1734; 2098, 2106-2108 »). Vérifiez les numéros d'articles avant de les alléguer dans un acte de procédure |
| `{{dossier.objet}}` | L'ancien « Objet », **renommé « Action »** — conservé comme alias : il donne le libellé de l'action, et non l'ancien texte libre |
| `{{dossier.valeur}}` | Valeur en litige, au format monétaire québécois (« 85 000,00 $ ») |
| `{{dossier.classe}}` | Classe selon la valeur (chiffre romain I–IV), déduite de la valeur en litige |
| `{{dossier.prescription}}` | Le libellé du délai de prescription confirmé (« 3 ans », « 90 jours », « Imprescriptible »). Il reste général : l'article du délai est donné par `{{dossier.reference}}` (et celui du droit d'action, par `{{dossier.fondement}}`), car une même durée vaut pour de nombreux articles |
| `{{dossier.droit_action}}` | Droit d'action : point de départ de la prescription (date au long) |
| `{{dossier.date_pour_agir}}` | Date pour agir : échéance de la prescription, calculée (date au long) |
| `{{dossier.prise_action}}` | Prise d'action : date du dépôt du recours, qui interrompt la prescription (art. 2892 C.c.Q.). Inscrite à la main, jamais calculée; une fois remplie, elle fait taire l'alerte de prescription |
| `{{dossier.type_mandat}}` | Libellé du type de mandat (« Judiciaire (ad litem) », « Service-conseils », « Général », « Spécial »). Un dossier qui portait un ancien type (« Transactionnel », « Consultatif », « Autre ») imprime le libellé actuel qui lui correspond |
| `{{dossier.type_dossier}}` | L'ancien « Type de dossier », **renommé « Domaine »** — conservé comme alias de `{{dossier.domaine}}` |
| `{{dossier.type_honoraires}}` | Libellé du type d'honoraires (« Horaire », « Forfaitaire », « Mixte », « Contingence », « Pro bono », « Aide juridique ») |
| `{{dossier.honoraires}}` | Type d'honoraires et taux réunis (« Horaire — 250,00 $/h », « Contingence — 25 % », « Mixte — 250,00 $/h + 5 000,00 $ + 25 % ») |
| `{{dossier.taux_horaire}}` | Taux horaire, au format monétaire québécois (« 250,00 $ ») |
| `{{dossier.forfait}}` | Forfait, au format monétaire québécois |
| `{{dossier.pourcentage}}` | Pourcentage de contingence (« 25 % ») — renseigné pour les types « Contingence » et « Mixte » |
| `{{dossier.notes_honoraires}}` | Notes libres sur l'entente d'honoraires |
| `{{dossier.ouverture}}` | Date d'ouverture (date au long) |
| `{{dossier.fermeture}}` | Date de fermeture (date au long; sans valeur tant que le dossier est ouvert) |
| `{{dossier.retention}}` | Date de fin de conservation des documents (« Rétention ») = date de fermeture + 7 ans (date au long) |

Les graphies accentuées `{{dossier.demandeur_avec_civilité}}` et `{{dossier.defendeur_avec_civilité}}` fonctionnent aussi.

#### Plusieurs parties : les familles de champs par rôle

Un dossier peut compter **autant de parties que nécessaire** de chaque côté, chacune avec son ou ses rôles. Un « mis en cause » n'est pas un défendeur : ces champs lisent donc **le rôle propre de chaque partie**, et non le côté où elle figure. Ils fonctionnent ainsi même dans un dossier dont le rôle général est vide, « intervenant » ou « autre », ce que ne permettent pas les *positions* demandeur/défendeur du tableau ci-dessus.

Chaque rôle a deux formes. La forme **en ligne** énumère les parties à la française : `A, B et C`. La forme **`_avec_adresse`** produit *un paragraphe par partie*, `Nom, adresse` : ces paragraphes étant séparés par une ligne vide, le paragraphe qui contient le champ est reproduit une fois pour chacun, **numérotation, retrait et style compris**. Placez ce champ seul dans un paragraphe de votre intitulé; il n'y a aucun marqueur à apprendre.

| Rôle | En ligne | Un paragraphe par partie (nom + adresse) |
|---|---|---|
| demandeur | `{{dossier.demandeurs}}` | `{{dossier.demandeurs_avec_adresse}}` |
| défendeur | `{{dossier.defendeurs}}` | `{{dossier.defendeurs_avec_adresse}}` |
| demandeur reconventionnel | `{{dossier.demandeurs_reconventionnels}}` | `{{dossier.demandeurs_reconventionnels_avec_adresse}}` |
| défendeur reconventionnel | `{{dossier.defendeurs_reconventionnels}}` | `{{dossier.defendeurs_reconventionnels_avec_adresse}}` |
| mis en cause | `{{dossier.mis_en_cause}}` | `{{dossier.mis_en_cause_avec_adresse}}` |
| intervenant | `{{dossier.intervenants}}` | `{{dossier.intervenants_avec_adresse}}` |
| appelant | `{{dossier.appelants}}` | `{{dossier.appelants_avec_adresse}}` |
| intimé | `{{dossier.intimes}}` | `{{dossier.intimes_avec_adresse}}` |
| requérant | `{{dossier.requerants}}` | `{{dossier.requerants_avec_adresse}}` |

Règles à surveiller ici :

- **Rien n'est inventé.** Si aucune partie ne porte le rôle, le champ reste sans valeur et imprime `[CHAMP MANQUANT : …]`. Un dossier de faillite dont les parties adverses sont `intimé` / `mis en cause` / `requérant` n'a **aucun** défendeur, et l'intitulé aurait tort d'en nommer un : utilisez le champ du rôle qui correspond.
- **Les anciens dossiers gardent leur sens.** Les rôles des parties existent depuis juillet 2026 et n'ont jamais été ajoutés rétroactivement. Lorsqu'*aucune* partie d'un côté ne porte de rôle, `{{dossier.demandeur}}` / `{{dossier.defendeur}}` nomment tout ce côté, comme auparavant. Un côté dont *certaines* parties portent un rôle est pris au mot : une partie de ce côté qui n'en porte pas, coclient ou confrère, n'y devient pas un second défendeur.
- **L'adresse vient de la fiche du contact**, choisie entre l'adresse personnelle et l'adresse professionnelle selon la même règle que pour `{{<slot>.adresse_complete}}`. Une partie sans adresse utilisable ne donne que son nom : un résultat incomplet, jamais faux.
- **Le champ fonctionne dans une cellule de tableau**, la mise en page de la plupart des intitulés : placez `{{dossier.defendeurs_avec_adresse}}` seul dans le paragraphe de la cellule de gauche, et « Défendeurs » dans celle de droite. C'est le *paragraphe* qui est reproduit, et non la rangée : la qualité inscrite en regard reste en place. (C'est pourquoi ces champs n'utilisent pas la rangée répétée `{{#region}}` : elle reproduit toute la rangée et aplatit les sauts de ligne, si bien qu'une adresse ne pourrait jamais occuper sa propre ligne.)
- **Une partie qui porte deux rôles figure sous chacun**, et c'est voulu : une défenderesse qui est aussi demanderesse reconventionnelle répond aux deux questions. Ne placez que les champs que votre intitulé doit nommer.
- `{{dossier.adresse_demandeur}}` / `{{dossier.adresse_defendeur}}` donnent **une seule** adresse : celle de la partie choisie dans la fenêtre de génération. Pour l'adresse de chaque partie, utilisez la forme `_avec_adresse`.

### `client.*`, `adverse.*`, `destinataire.*` (créneaux de partie)

Chacun des trois créneaux (client, partie adverse, destinataire) offre les **mêmes 14 champs**. Remplacez `<slot>` par `client`, `adverse` ou `destinataire` :

| Champ | Valeur |
|---|---|
| `{{<slot>.nom_complet}}` | Nom complet, **sans civilité**; pour une organisation, sa dénomination sociale |
| `{{<slot>.nom_complet_avec_civilite}}` | Nom complet **avec civilité** : le titre inscrit à la fiche du contact, jamais le suffixe (« Ad. E. », « j.c.s. ») ni les autres prénoms (la graphie accentuée `…_civilité` fonctionne aussi) |
| `{{<slot>.prenom}}` | Prénom (personne physique seulement) |
| `{{<slot>.nom}}` | Nom de famille (personne physique seulement) |
| `{{<slot>.organisation}}` | Nom de l'organisation |
| `{{<slot>.adresse_civique}}` | Adresse civique (la rue, suivie s'il y a lieu d'une virgule et de l'appartement ou du bureau) |
| `{{<slot>.ville}}` | Ville |
| `{{<slot>.province}}` | Province |
| `{{<slot>.code_postal}}` | Code postal |
| `{{<slot>.pays}}` | Pays |
| `{{<slot>.adresse_complete}}` | Adresse complète, sur une seule ligne |
| `{{<slot>.courriel}}` | Courriel, professionnel ou personnel selon l'adresse retenue |
| `{{<slot>.telephone}}` | Téléphone, mis en forme, selon l'adresse retenue (cellulaire → domicile → travail, ou travail d'abord) |
| `{{<slot>.numero_barreau}}` | Numéro du Barreau |

> **Choix de l'adresse (préférence, puis repli) :** le rôle du contact décide du bloc d'adresse essayé **en premier** — le bloc *professionnel* pour un avocat, un expert, un huissier, un notaire, un sténographe ou un fournisseur, le bloc *personnel* pour tous les autres (clients compris). **L'autre bloc sert de repli** quand le bloc préféré ne contient aucune adresse, si bien qu'une **personne morale** s'imprime toujours : le formulaire du contact masque le bloc personnel d'une organisation (son adresse ne peut y être saisie que sous « Adresse », dans le bloc professionnel), alors que le portail client inscrit l'adresse d'une entreprise dans le bloc personnel. Les deux sont légitimes.
>
> Le courriel et le téléphone suivent le bloc retenu. Pour le téléphone, l'ordre est cellulaire → domicile → travail pour une personne physique à qui l'on écrit à domicile, et travail → cellulaire → domicile pour le bloc professionnel (et pour une organisation). Le bloc professionnel d'un client peut en effet contenir les coordonnées de son **employeur** : une lettre au client ne doit pas porter le numéro de l'employeur. Pour une personne physique, l'adresse de facturation d'une facture suit le même choix. Ces règles valent pour tous les champs d'adresse, de courriel et de téléphone du créneau.

### `cabinet.*` (le cabinet)

`{{cabinet.nom}}` · `{{cabinet.organisation}}` · `{{cabinet.adresse_civique}}` · `{{cabinet.ville}}` · `{{cabinet.province}}` · `{{cabinet.code_postal}}` · `{{cabinet.telephone}}` · `{{cabinet.telecopieur}}` · `{{cabinet.courriel}}`

> **`nom`, c'est vous; `organisation`, c'est le cabinet.** `{{cabinet.nom}}`, c'est votre nom (« Me Jason Poirier Lavoie »), celui qui signe : un acte de procédure, une lettre, une vérification d'identité. `{{cabinet.organisation}}` est le nom commercial du cabinet (« Poirier Lavoie, avocat »), pour un en-tête de lettre ou la désignation du cabinet. Laissé vide, `organisation` prend la valeur de `nom`; jamais l'inverse.
>
> Tous ces champs se modifient dans **Paramètres → Profil du cabinet**. Le nom affiché dans le *portail client*, lui, provient de la configuration de déploiement et ne se modifie pas à cet endroit.

### `date.*`

| Champ | Valeur |
|---|---|
| `{{date.aujourdhui}}` | La date du jour, écrite au long (« 25 avril 2026 »; `1er` pour le premier du mois) |
| `{{date.aujourdhui_iso}}` | La date du jour, au format ISO `AAAA-MM-JJ` |

---

## 3. Alias courts

Les alias courts sont des noms sans préfixe qui renvoient chacun à un champ du catalogue : un même jeu de gabarits sert ainsi à la fois dans l'application et avec des compétences (*skills*) Claude externes. Un alias court **l'emporte** sur un champ à préfixe de même graphie.

| Alias | Renvoie à |
|---|---|
| `{{district}}` | `dossier.district` |
| `{{numero_dossier}}` | `dossier.numero_cour` |
| `{{tribunal}}` | `dossier.tribunal` |
| `{{chambre}}` | `dossier.chambre` |
| `{{référence_interne}}` | `dossier.reference_interne` |
| `{{intitulé_dossier}}` | `dossier.titre` |
| `{{sommaire}}` | `dossier.sommaire` |
| `{{rôle}}` | `dossier.role_feminin` (rôle au **féminin**, et non le rôle tel qu'il est enregistré) |
| `{{demandeur}}` / `{{défendeur}}` | `dossier.demandeur` / `dossier.defendeur` (sans civilité) |
| `{{demandeur_avec_civilité}}` / `{{demandeur_avec_civilite}}` | `dossier.demandeur_avec_civilite` |
| `{{défendeur_avec_civilité}}` / `{{défendeur_avec_civilite}}` | `dossier.defendeur_avec_civilite` |
| `{{adresse_demandeur}}` / `{{adresse_défendeur}}` | `dossier.adresse_demandeur` / `dossier.adresse_defendeur` |
| `{{valeur}}` | `dossier.valeur` |
| `{{classe}}` | `dossier.classe` |
| `{{prescription}}` | `dossier.prescription` |
| `{{droit_action}}` | `dossier.droit_action` |
| `{{date_pour_agir}}` | `dossier.date_pour_agir` |
| `{{prise_action}}` | `dossier.prise_action` |
| `{{domaine}}` | `dossier.domaine` |
| `{{action}}` | `dossier.action` |
| `{{objet}}` | `dossier.objet` (→ le libellé de l'action) |
| `{{précision}}` / `{{precision}}` | `dossier.precision` |
| `{{délai}}` / `{{delai}}` | `dossier.delai` |
| `{{point_départ}}` / `{{point_depart}}` | `dossier.point_depart` |
| `{{référence_action}}` / `{{reference_action}}` | `dossier.reference` |
| `{{fondement}}` / `{{référence_fondement}}` / `{{reference_fondement}}` | `dossier.fondement` |
| `{{type_mandat}}` | `dossier.type_mandat` |
| `{{type_dossier}}` | `dossier.type_dossier` (→ le libellé du domaine) |
| `{{date_ouverture}}` / `{{date_fermeture}}` | `dossier.ouverture` / `dossier.fermeture` |
| `{{rétention}}` / `{{retention}}` | `dossier.retention` |
| `{{ville_procédure}}` / `{{ville_lettre}}` | `cabinet.ville` |
| `{{date_procédure}}` / `{{date_lettre}}` | `date.aujourdhui` |
| `{{prénom_récipient}}` | `destinataire.prenom` |
| `{{nom_récipient}}` | `destinataire.nom` |
| `{{cabinet_récipient}}` | `destinataire.organisation` |
| `{{adresse_civique_récipient}}` | `destinataire.adresse_civique` |
| `{{ville_récipient}}` | `destinataire.ville` |
| `{{province_récipient}}` | `destinataire.province` |
| `{{code_postal_récipient}}` | `destinataire.code_postal` |
| `{{pays_récipient}}` | `destinataire.pays` |

---

## 4. Champs à saisir (demandés à la génération, sans source de données)

Ce sont de courtes mentions propres à la lettre, demandées dans la fenêtre de génération. Laissé vide, un champ sans valeur par défaut imprime `[À COMPLÉTER : name]`.

| Champ | Valeur par défaut / choix |
|---|---|
| `{{procédure}}` | texte libre (vide) |
| `{{disposition}}` | texte libre (vide) |
| `{{objet_lettre}}` | texte libre (vide) |
| `{{référence_externe}}` | texte libre (vide) |
| `{{pièces_jointes}}` | **`Aucune`** par défaut |
| `{{privilège}}` | liste de choix : `SOUS TOUTES RÉSERVES` · `SOUS TOUTES RÉSERVES ET SANS PRÉJUDICE` · `SANS PRÉJUDICE` · `PERSONNEL ET CONFIDENTIEL` · `CONFIDENTIEL` · `PRIVILÉGIÉ ET CONFIDENTIEL` · `—` · **`(aucune mention)`** |
| `{{transmission_lettre}}` | liste de choix : `courriel` · `huissier` · `poste recommandée` · `télécopieur` |

**Deux façons de ne rien dire, qui ne reviennent pas au même.** Choisir **« (aucune mention) »** n'imprime *rien du tout*; ne rien choisir dans la liste imprime le marqueur bien visible `[À COMPLÉTER : privilège]`. Le choix `—` imprime le tiret cadratin lui-même, pour un en-tête qui réserve une ligne visible à la mention.

Une valeur hors de la liste de choix d'un champ est **refusée**, avec un message qui nomme le champ.

---

## 5. Champs laissés tels quels

Les champs suivants ne sont délibérément **ni remplis ni demandés** : ils restent tels quels, sous la forme `{{name}}`, dans le document produit, pour que vous les placiez et les remplissiez vous-même dans Word.

- `{{civilité}}` — la civilité du destinataire. (Elle a sa place dans une lettre, jamais dans un acte de procédure : c'est donc à vous de la placer.)
- `{{salutations}}` — la formule de salutation finale.
- **Tout bloc en MAJUSCULES**, pour du contenu juridique libre — par exemple `{{FAITS}}`, `{{CONCLUSIONS}}`, `{{MOYENS}}`. **Attention : les majuscules ne suffisent pas à laisser un nom tel quel.** Les majuscules sont ignorées pour reconnaître les champs automatiques comme les champs à saisir; les sept noms de la section 4 sont donc demandés, qu'ils soient écrits en majuscules ou non : `{{PRIVILÈGE}}` reçoit sa liste de choix, et `{{DISPOSITION}}` / `{{PROCÉDURE}}` sont demandés comme champs à saisir au lieu d'être laissés pour Word. Donnez à vos blocs un nom qui ne figure pas parmi ces sept.
- **Tout nom inconnu** — tout ce qui ne correspond ni à un champ automatique (directement ou par un alias court) *ni à un champ à saisir*, sans égard aux majuscules dans les deux cas.

### Par le connecteur (`fill_gabarit`)

Un champ laissé tel quel est aussi ce que **Claude** peut rédiger, par l'outil `fill_gabarit` du connecteur : c'est un *bloc*. Tout le reste revient à l'application.

**Ce qu'un appel peut fournir — et rien d'autre :**

| Argument | Description | Limites |
|---|---|---|
| `template_id` | Le gabarit. **Type « gabarit » seulement** — un gabarit « Note d'honoraires », « Note de crédit » ou « Note (impression) » est refusé, car chacun a son propre circuit : pour une note d'honoraires, le bouton « Note d'honoraires (Word) » de la facture dans l'application, ou `create_document` avec `source: invoice_note` par le connecteur; pour une note de crédit, le bouton « Générer la note de crédit » d'une radiation, sur la fiche de la facture, dans l'application seulement (voir la section 7); pour l'impression d'une note, `create_document` avec `source: markdown` par le connecteur (voir la section 8). Un « Modèle (sans champs) » — un modèle de contrat, de type `modele` — est refusé lui aussi : il ne se remplit jamais; il se télécharge et s'adapte dans Word | — |
| `dossier_id` | **Obligatoire.** Ses données remplissent tous les champs automatiques, et le document est enregistré comme NOUVEAU document dans le sous-dossier « Projets » de CE dossier — **toujours là** : jamais dans un autre sous-dossier, jamais en téléchargement. Dans l'arborescence par défaut, « Projets » se trouve sous « Interne » (« Interne › Projets »); un dossier qui n'a pas encore reçu cette arborescence garde son ancien sous-dossier « Projets » à la racine, utilisé là où il se trouve | — |
| `client_id` / `adverse_id` | Les créneaux « client » / « adverse » — une partie **de ce dossier**, à fournir quand le dossier en compte plusieurs et que le gabarit lit ce créneau (jamais devinée, jamais la première par défaut) | — |
| `destinataire_id` | Le créneau « destinataire » — n'importe quel contact; aucun par défaut | — |
| `blocs` | `[{nom, contenu, markdown?}]` — pour les champs laissés tels quels seulement | ≤ 12 blocs, ≤ 20 000 caractères chacun, 60 000 en tout |
| `champs_manuels` | `[{nom, valeur}]` — les champs de la section 4; leur liste de choix est imposée, et le choix « (aucune mention) » n'imprime rien | ≤ 12, ≤ 2 000 caractères chacun |

**Claude lit d'abord le gabarit.** `list_templates` avec `template_id` (ainsi que le `dossier_id` et les identifiants des créneaux) énumère les champs AUTOMATIQUES du gabarit, chacun avec un indicateur « resolved » qui dit si une valeur existe (**jamais la valeur elle-même**), ses champs À SAISIR avec leurs choix, et ses BLOCS (noms exacts). Un champ automatique sans valeur imprime `[CHAMP MANQUANT : name]` : c'est une lacune du dossier, à signaler ou à corriger dans le dossier même, et non un vide à combler par un bloc. Une **partie inscrite au dossier qui ne peut pas être lue** (une erreur de lecture passagère) n'est PAS une telle lacune : `fill_gabarit` refuse alors et n'enregistre rien — « réessayez » — plutôt que d'imprimer les marqueurs à la place des noms et des adresses de l'intitulé. (La fenêtre de génération, elle, les imprime : elle vous montre chaque valeur avant de générer.)

- **Seuls les champs laissés tels quels reviennent à Claude.** Les champs automatiques sont remplis par l'application à partir du dossier et de ses parties (un bloc qui porte le nom de l'un d'eux est refusé : il ne le remplace jamais); les champs à saisir se fournissent par leur propre liste, où leurs choix sont imposés. Les noms sont comparés **à l'identique**, tels que `list_templates` les rapporte : ici, les majuscules comptent.
- **Les paragraphes d'un bloc sont séparés par une LIGNE VIDE**; un simple saut de ligne devient une espace. Chaque paragraphe est une copie du paragraphe hôte : un `{{FAITS}}` seul dans un paragraphe **numéroté** donne de vrais numéros Word, continus — c'est pourquoi un bloc ne doit porter **aucune numérotation propre**.
- **`markdown: true`** fait passer un bloc par la conversion en mise en forme Word de la section 8 (la numérotation propre du paragraphe hôte étant neutralisée). Il lui faut le même hôte que `{{note.contenu}}` : seul dans son paragraphe, dans le corps du document. Sinon, il s'imprime en texte brut, symboles Markdown visibles, et l'outil le signale (`blocs_demoted`).

**Numéros de Word ou numéros calculés — un chemin ou l'autre, jamais les deux :**

| | Bloc simple (par défaut) | `markdown: true` |
|---|---|---|
| Qui numérote | **Word** — la numérotation du paragraphe hôte s'applique à chacun des paragraphes que séparent les lignes vides | **Le convertisseur** — une liste numérotée Markdown (`1.`, `2.`) est numérotée en TEXTE, avec des numéros calculés à la génération |
| La numérotation de l'hôte | Héritée, une fois par paragraphe | Neutralisée (retirée explicitement : jamais héritée, donc rien n'est numéroté deux fois) |
| Quand vous retouchez dans Word | Les numéros se mettent à jour d'eux-mêmes et continuent la liste à laquelle appartient le paragraphe hôte | Les numéros sont du simple texte : insérer ou retirer un élément ne renumérote PAS les autres, et chaque liste Markdown est numérotée séparément |
| À utiliser pour | Allégations numérotées, conclusions — tout ce que vous aurez à renuméroter | Structure interne : titres, gras, liste à puces, tableau |

Un « 1. » écrit à la main dans un bloc simple placé dans un paragraphe numéroté imprime le numéro DEUX FOIS (celui de Word, puis celui du texte). Une limite, commune avec la section 8 : la numérotation portée par le seul STYLE Word du paragraphe (« Liste numérotée » / « List Number », sans numéro appliqué au paragraphe lui-même) est encore héritée par un bloc `markdown: true` — donnez à ce paragraphe un style ordinaire.

- **Un bloc ou la valeur d'un champ à saisir ne peut jamais contenir `{{` ni `}}`** — l'application lirait un `{{dossier.demandeur}}` qui s'y trouverait comme un champ et y imprimerait les données du dossier. Un tel texte est refusé, et non neutralisé. Un bloc **Markdown** (comme le Markdown de `create_document`) est aussi jugé une fois MIS EN FORME : un échappement `\{` ou une référence de caractère `&#123;` / `&lbrace;` imprime une accolade, si bien qu'un texte qui imprimerait `{{…}}` est refusé lui aussi.
- Un bloc que Claude ne rédige pas reste `{{name}}` dans le document, à compléter dans Word, et le résultat dit lesquels (`blocs_left_verbatim`, relu dans le fichier produit — un champ fragmenté par Word y figure aussi).

---

## 6. Note d'honoraires seulement (type « Note d'honoraires (facture) »)

Le bouton « Note d'honoraires (Word) » de la facture — ainsi que l'outil `create_document` du connecteur avec `source: invoice_note`, qui passe par la MÊME génération — remplit le gabarit de ce type que vous avez **désigné comme actif** sur sa page (« Désigner comme gabarit actif »), jamais simplement le plus récent. Si aucun gabarit n'est désigné, la génération est refusée et le message l'indique.

La note est enregistrée comme NOUVEAU document du dossier de la facture, dans le sous-dossier « Factures », sous « Mandat » (« Mandat › Factures »). L'application retrouve ce sous-dossier système par son rôle et le crée, avec « Mandat », à la première utilisation; s'il ne peut être obtenu, la génération est refusée : la note n'est jamais enregistrée à la racine du dossier. La note garde son nom « … Projet … ». Une note générée avant l'arborescence par défaut des documents (septembre 2026) a été enregistrée dans le sous-dossier « Projets », à côté des documents tirés des gabarits : elle y reste, et le connecteur la renvoie encore, sans en enregistrer une autre, lorsqu'elle est identique à ce qu'il générerait.

Un gabarit de note d'honoraires peut utiliser **tout ce qui précède** pour son en-tête (`dossier.*`, `destinataire.*`, `cabinet.*`, `date.*` et leurs alias courts — le créneau destinataire y est le client de la facture), **plus** les champs qui suivent.

Toutes les valeurs `facture.*` de montant, de taux, de date et d'heures arrivent **déjà mises en forme** (espace insécable pour les milliers, virgule décimale, ` $` à la fin). Les chiffres sont lus dans la facture enregistrée, jamais recalculés.

### Champs `facture.*`

| Champ | Valeur |
|---|---|
| `{{facture.numero}}` | Numéro de la facture, tel qu'enregistré |
| `{{facture.date}}` | Date de la facture (date au long) |
| `{{facture.date_echeance}}` | Date d'échéance |
| `{{facture.sous_total_honoraires}}` | Sous-total des honoraires |
| `{{facture.sous_total_debours_tx}}` | Sous-total des débours taxables |
| `{{facture.sous_total_debours_ntx}}` | Sous-total des débours non taxables |
| `{{facture.total_honoraires}}` | Total des honoraires (= `sous_total_honoraires`) |
| `{{facture.total_debours_tx}}` | Total des débours taxables (= `sous_total_debours_tx`) |
| `{{facture.total_debours_ntx}}` | Total des débours non taxables (= `sous_total_debours_ntx`) |
| `{{facture.total_avant_taxes}}` | Sous-total avant taxes |
| `{{facture.tps_taux}}` | Taux de la TPS (« 5 % ») |
| `{{facture.tps_numero}}` | Numéro d'inscription à la TPS |
| `{{facture.tps_montant}}` | Montant de la TPS |
| `{{facture.tvq_taux}}` | Taux de la TVQ (« 9,975 % ») |
| `{{facture.tvq_numero}}` | Numéro d'inscription à la TVQ |
| `{{facture.tvq_montant}}` | Montant de la TVQ |
| `{{facture.total_apres_taxes}}` | Total après taxes |
| `{{facture.avances_fideicommis}}` | Avances en fidéicommis appliquées à la facture, en déduction **entre parenthèses** (« (1 150,00) $ ») |
| `{{facture.solde}}` | Solde dû **à l'émission** — un paiement ne le réduit jamais |
| `{{facture.solde_restant}}` | Solde restant après les paiements inscrits et les radiations en vigueur au moment où la note est générée — le « Solde » de la page de la facture; zéro sur une facture dont le solde est radié. Seulement dans un gabarit qui imprime les paiements (voir *Paiements*, plus bas) |
| `{{facture.solde_fideicommis}}` | Solde en fidéicommis du client dans le dossier de la facture au moment où la note est générée, écritures non encore compensées comprises (voir *Solde en fidéicommis*, plus bas) |
| `{{facture.nombre_heures}}` | Total des heures facturées (« 0,50 ») |
| `{{facture.taux_horaire}}` | Taux horaire : le taux facturé, s'il est le même pour toutes les lignes; sinon celui du dossier; sinon vide |

> `sous_total_debours_tx` + `sous_total_debours_ntx` = le sous-total des débours de la facture.

### Rangées répétées

| Marqueur de la rangée | Champs de la rangée |
|---|---|
| `{{#ligne_honoraire}}` | `{{h.date}}` · `{{h.description}}` · `{{h.temps}}` |
| `{{#ligne_debours_tx}}` (débours taxables) | `{{d.date}}` · `{{d.description}}` · `{{d.cout}}` |
| `{{#ligne_debours_ntx}}` (débours non taxables) | `{{d.date}}` · `{{d.description}}` · `{{d.cout}}` |
| `{{#ligne_paiement}}` (paiements — voir plus bas) | `{{p.date}}` · `{{p.source}}` · `{{p.montant}}` |

Les deux rangées de débours ont exactement les mêmes champs `d.*`; seules diffèrent les lignes de la facture qui remplissent chacune (débours taxables ou non taxables). Les champs d'une rangée portent le préfixe `h.`, `d.` ou `p.` : ils n'entrent ainsi jamais en conflit avec les autres champs du gabarit.

### Conditions

| Condition | Vraie lorsque |
|---|---|
| `{{?si_honoraires}}` … `{{/si_honoraires}}` | il y a au moins une ligne d'honoraires |
| `{{?si_debours_tx}}` … `{{/si_debours_tx}}` | il y a au moins un débours taxable |
| `{{?si_debours_ntx}}` … `{{/si_debours_ntx}}` | il y a au moins un débours non taxable |
| `{{?si_paiements}}` … `{{/si_paiements}}` | il y a au moins une rangée de paiement (seulement dans un gabarit qui imprime les paiements — voir plus bas) |

Encadrez le tableau de chaque section entre les marqueurs de sa condition : une section vide disparaît alors proprement.

### Paiements (`ligne_paiement`)

La rangée répétée des paiements énumère ce qui a été payé — ou radié — sur la facture **au moment où la note est générée**. Une note générée à l'émission ne montre aucun paiement : générez-la de nouveau après un paiement ou une radiation. Le bouton de la facture enregistre une nouvelle note à chaque clic, et le connecteur en enregistre une nouvelle dès que les paiements ont changé.

- **Les rangées.** La provision d'une facture importée vient en premier (les « Avances en fidéicommis » déduites à son émission), datée du jour de la facture, avec la source « Fidéicommis ». Suivent les encaissements imputés à la facture dans le registre de l'administration, du plus ancien au plus récent. Un paiement d'honoraires tiré du fidéicommis en fait partie, puisqu'il est aussi inscrit à ce registre. Un encaissement contre-passé ou annulé n'est jamais imprimé.
- **La radiation.** La radiation du solde d'une facture en retard est une réduction du prix que la note **montre** au client, documentée par une note de crédit numérotée (« 2026-NC001 », une série par année, jamais réutilisée; son document Word se génère depuis la fiche de la facture, voir la section 7) : chaque radiation en vigueur occupe une rangée, datée du jour où elle a été inscrite, avec la source « Note de crédit » suivie de son numéro, et son montant, taxes comprises. Elle suit les encaissements de son jour et mène le solde restant à zéro. Une radiation annulée n'est jamais imprimée, ni son annulation : sa note de crédit garde son numéro, annulée. La rangée est celle de votre gabarit : rien à y ajouter.
- **`{{p.date}}`** — la date de l'encaissement au registre (celle qu'affiche la page de la facture), écrite au long; pour une radiation, le jour où elle a été inscrite.
- **`{{p.source}}`** — « Fidéicommis » pour un paiement tiré du fidéicommis, « Note de crédit 2026-NC001 » (son numéro) pour une radiation du solde. Sinon, le mode de paiement inscrit : Chèque, Virement, Prélèvement, Dépôt direct, Carte, Comptant, et « Paiement » pour « Autre ».
- **`{{p.montant}}`** — entre parenthèses, sous la même forme de déduction que `{{facture.avances_fideicommis}}` : « (500,00) $ ».
- **Le solde.** `{{facture.solde}}` demeure le montant dû à l'émission. Le solde après les rangées est `{{facture.solde_restant}}` : sur la note, le TOTAL APRÈS TAXES moins les rangées donne exactement ce solde.
- **Placez la rangée répétée À LA PLACE de la ligne « Avances en fidéicommis »**, jamais à côté. La rangée reprend déjà la provision : garder les deux la déduirait deux fois.

Dans le tableau des totaux :

| | |
|---|---|
| `TOTAL APRÈS TAXES` | `{{facture.total_apres_taxes}}` |
| `{{#ligne_paiement}}{{p.date}} — {{p.source}}` | `{{p.montant}}` |
| `SOLDE` | `{{facture.solde_restant}}` |

**Quand les paiements sont lus.** Uniquement pour un gabarit qui les imprime, c'est-à-dire qui nomme `{{p.date}}`, `{{p.source}}`, `{{p.montant}}` ou `{{facture.solde_restant}}`. Tout autre gabarit est rempli sans les lire. Pour la même raison, `{{?si_paiements}}` ne fonctionne que dans un gabarit qui imprime aussi l'un de ces champs : ses marqueurs ne font pas partie de la liste des champs que l'application retient pour un gabarit, et ailleurs ils restent tels que vous les avez tapés.

**Refus** — rien n'est généré, et le message en donne la raison, lorsque :

- les paiements n'ont pas pu être lus;
- le total des encaissements inscrits ne correspond pas au montant payé de la facture;
- la facture a été marquée « payée » à la main et il reste un solde. Inscrivez d'abord les encaissements manquants.

### Solde en fidéicommis (`facture.solde_fideicommis`)

Le solde détenu en fidéicommis pour le client de la facture **dans le dossier de la facture** (la carte-client de ce client pour ce dossier), au moment où la note est générée. Les écritures que la banque n'a pas encore compensées comptent : un paiement d'honoraires qui vient d'être inscrit est donc déjà déduit. Inscrivez d'abord le paiement tiré du fidéicommis (« Paiement d'honoraires »), puis générez la note : sa rangée de paiement montre le montant tiré, et ce champ, ce qui reste. Les fonds que le client détient dans un autre dossier ne comptent jamais.

- Il s'imprime comme un montant ordinaire, sans parenthèses : « 1 150,00 $ ».
- Le client est celui que nomme la facture. Pour une facture importée qui n'en nomme aucun, c'est le client du dossier s'il est le seul, selon la même règle que pour un paiement d'honoraires. À défaut de l'un comme de l'autre, la génération est refusée.
- Comme les paiements, il n'est lu que pour un gabarit qui l'imprime.

---

## 7. Note de crédit seulement (type « Note de crédit (radiation) »)

La radiation du solde d'une facture en retard est une réduction du prix que le client reçoit documentée par une **note de crédit**, numérotée à l'inscription de la radiation (« 2026-NC001 », une série par année, jamais réutilisée). Sur la fiche de la facture, chaque radiation en vigueur offre le bouton « Générer la note de crédit » : il remplit le gabarit de ce type que vous avez **désigné comme actif** sur sa page (« Désigner comme gabarit actif »), jamais simplement le plus récent. Si aucun gabarit n'est désigné, la génération est refusée et le message l'indique. Le connecteur, lui, ne génère aucune note de crédit.

La note de crédit est enregistrée comme NOUVEAU document du dossier de la facture, au même endroit que la note d'honoraires : dans « Mandat › Factures ». Son nom suit celui de la note d'honoraires : « … Projet », le nom du gabarit, puis le numéro de la note de crédit. Un nouveau clic, tant que rien de ce qu'elle imprime n'a changé, rend la note de crédit déjà versée au lieu d'en verser une seconde; une nouvelle version du gabarit en verse une nouvelle.

Une radiation annulée ne génère plus de note de crédit : sa note de crédit garde son numéro, annulée, et un document déjà versé pour elle reste au dossier.

Un gabarit de note de crédit peut utiliser tout ce qui précède pour son en-tête (`dossier.*`, `destinataire.*`, `cabinet.*`, `date.*` et leurs alias courts — le créneau destinataire y est le client de la facture, comme pour la note d'honoraires), **plus** les champs qui suivent, mis en forme comme ceux de la note d'honoraires. Aucune rangée répétée ni condition ne s'y remplit, ni aucun autre champ `facture.*` : laissés dans le gabarit, ils restent tels quels.

### Champs `nc.*`

| Champ | Valeur |
|---|---|
| `{{nc.numero}}` | Numéro de la note de crédit (« 2026-NC001 ») |
| `{{nc.date}}` | Date de la radiation, soit le jour où elle a été inscrite (date au long) |
| `{{nc.motif}}` | Le motif inscrit à la radiation, tel quel; sans motif, rien ne s'imprime. Ne le placez que si ce motif peut être lu par le client |
| `{{nc.montant_avant_taxes}}` | Montant de la réduction avant taxes : le total radié, moins la TPS et la TVQ |
| `{{nc.tps}}` | La TPS comprise dans le total radié — « 0,00 $ » quand la radiation ne réclame pas la TPS/TVQ : le montant avant taxes est alors le total radié |
| `{{nc.tvq}}` | La TVQ comprise dans le total radié, selon la même règle |
| `{{nc.total}}` | Le total radié, taxes comprises |

### Champs de la facture

| Champ | Valeur |
|---|---|
| `{{facture.numero}}` | Numéro de la facture que la note de crédit réduit |
| `{{facture.date}}` | Date de cette facture (date au long) |
| `{{facture.tps_numero}}` / `{{facture.tvq_numero}}` | Les numéros d'inscription à la TPS et à la TVQ, tels qu'ils figurent sur la facture |
| `{{facture.solde_restant}}` | Le solde de la facture après la note de crédit : zéro, la radiation soldant la facture |

Exemple de mise en page :

| | |
|---|---|
| `NOTE DE CRÉDIT {{nc.numero}}` | `{{nc.date}}` |
| `Facture {{facture.numero}} du {{facture.date}}` | |
| `Réduction avant taxes` | `{{nc.montant_avant_taxes}}` |
| `TPS` | `{{nc.tps}}` |
| `TVQ` | `{{nc.tvq}}` |
| `TOTAL DU CRÉDIT` | `{{nc.total}}` |
| `SOLDE DE LA FACTURE` | `{{facture.solde_restant}}` |

---

## 8. Impression d'une note seulement (type « Note (impression) »)

Le gabarit d'**impression d'une note** est le fichier `.docx` que remplit le bouton « Imprimer (Word) » de la page d'une note (et de l'onglet Analyse); le résultat vous est remis en **téléchargement direct** et n'est jamais enregistré parmi les documents du dossier. Téléversez-le dans « Gabarits » avec le type **« Note (impression) »**, puis **désignez-le** sur sa page (« Désigner comme gabarit actif ») : c'est le gabarit DÉSIGNÉ de ce type qui sert, jamais simplement le plus récemment modifié, et s'il n'y en a aucun, l'impression est refusée avec un message qui indique comment y remédier. Ce gabarit peut employer tous les champs qui précèdent (`dossier.*`, `cabinet.*`, `date.*`, alias courts — mais aucun créneau destinataire), **et, en plus, ceux-ci :**

### Champs `note.*`

| Champ | Valeur |
|---|---|
| `{{note.titre}}` | Le titre de la note, tel quel. |
| `{{note.categorie}}` | Le libellé de la catégorie (« Stratégie », « Rencontre », …). |
| `{{note.date}}` | Date de création — le jour à **Montréal**, écrite au long (« 1er août 2026 »). |
| `{{note.date_maj}}` | Date de la dernière modification, même format — **vide si c'est le jour de la création** (comme la mention « Modifiée » à l'écran). |
| `{{note.dossier}}` | `N° — Titre` du dossier de la note; **« Général »** pour une note sans dossier (toujours rempli — préférez-le aux champs `dossier.*` si votre gabarit doit aussi servir aux notes générales, dont les champs `dossier.*` s'impriment `[CHAMP MANQUANT : …]`). |
| `{{note.contenu}}` | **Le champ enrichi** — le corps Markdown de la note, converti en véritable mise en forme Word. |

### Ce que produit `{{note.contenu}}`

La conversion reproduit le rendu À L'ÉCRAN (même traitement du Markdown) : les titres (en gras, en tailles échelonnées à partir de la taille de police de votre gabarit), le **gras** et l'*italique*, le `code` en ligne et les blocs de code (Consolas, sur fond ombré), les listes à puces et numérotées (puces en texte et numéros calculés — imprimables, mais les styles de Word ne peuvent pas les reformater), les citations `>` (bordure à gauche), les filets `---`, les sauts de ligne simples rendus comme de vrais sauts de ligne, les liens en texte souligné suivi de leur adresse URL, et **les tableaux Markdown en vrais tableaux Word** (avec bordures, ligne d'en-tête ombrée, alignement `:---:` respecté, colonnes de largeur égale sur toute la largeur de la page).

### Règles à surveiller (impression d'une note)

- **`{{note.contenu}}` doit être SEUL dans son propre paragraphe** du gabarit. Ce paragraphe entier est remplacé par le contenu converti; sa mise en forme (police, taille, justification) sert de base au texte. Si le champ partage sa ligne avec un autre texte, le contenu se dégrade en texte brut (symboles Markdown visibles) — le document est tout de même produit.
- **Toute la mise en forme de ce paragraphe se transmet au contenu, SAUF sa numérotation Word** : un `{{note.contenu}}` placé dans un paragraphe de liste numérotée ne numérote pas une seconde fois chaque titre, élément et cellule de tableau devant les numéros que la conversion calcule elle-même. Si vous voulez plutôt la numérotation de Word sur chaque paragraphe, c'est ce que fait le remplissage simple d'un champ ordinaire placé dans un paragraphe numéroté, quand sa valeur sépare ses paragraphes par des lignes vides (voir la section 1). Une limite : la numérotation qui ne vient que du STYLE Word du paragraphe (« Liste numérotée » / « List Number », sans numéro appliqué au paragraphe lui-même) est encore héritée — donnez à ce paragraphe un style ordinaire.
- Ne placez jamais `{{note.contenu}}` dans un en-tête ou un pied de page : il y est laissé tel quel.
- Les autres champs `note.*` sont des champs simples et fonctionnent partout.
- **Le connecteur imprime avec ce même gabarit** (`create_document` avec `source: markdown`) : le Markdown de Claude remplit `{{note.contenu}}` et son titre, `{{note.titre}}`; le résultat est enregistré comme NOUVEAU document dans le sous-dossier « Projets » (sous « Interne » dans l'arborescence par défaut), à moins que Claude n'indique un autre sous-dossier du dossier (l'impression depuis l'application reste un téléchargement). Seul le gabarit que vous avez désigné comme **actif** sert — s'il n'y en a aucun, le connecteur refuse; là où `{{note.contenu}}` ne peut pas recevoir de mise en forme, il REFUSE plutôt que d'enregistrer un document rempli de symboles Markdown; et là où le champ n'est pas du tout dans le CORPS du document (seulement dans un en-tête ou un pied de page, ou fragmenté par Word), il refuse aussi — constat fait en relisant le fichier produit — plutôt que d'enregistrer un document sans le texte. Un texte rédigé par Claude n'a ni catégorie de note ni date de modification : `{{note.categorie}}` et `{{note.date_maj}}` s'y impriment vides.

---

## 9. Transformer une lettre en gabarit (par le connecteur)

Une lettre déjà rédigée pour UN dossier peut devenir un gabarit du cabinet : chaque texte propre à ce dossier — un nom, un numéro de dossier, une adresse — est remplacé par le champ qui le remplira pour chaque dossier à venir. Claude y procède en deux appels au connecteur, et rien n'est écrit avant le second.

1. **`preview_templatize`** (une lecture) reçoit le `.docx` enregistré (trouvé par `list_documents`) et une liste de paires `{literal, placeholder}` : le texte à remplacer et le champ qui le remplace. Pour chaque paire, il indique combien de fois le texte serait remplacé, **partie par partie** (corps, en-têtes, pieds de page), ce qu'il **laisserait en place**, la **classification** du champ (automatique — l'application le remplit; à saisir — demandé au moment de la génération; laissé tel quel — à compléter dans Word, et c'est aussi là qu'aboutit un nom du catalogue **mal orthographié**), ainsi que les identifiants du dossier d'origine — et les textes à remplacer eux-mêmes — qui **subsisteraient** dans le résultat. Il ne renvoie jamais le texte de la lettre.
2. **Ajuster et relancer l'aperçu** jusqu'à obtenir `ready_to_create` : ajouter une paire pour chaque variante encore signalée, corriger un nom de champ classé comme laissé tel quel.
3. **`create_template`** avec les mêmes `substitutions`, chacune portant `expected_occurrences` = le nombre indiqué par l'aperçu (`substituted`). Un seul nombre différent fait refuser l'appel **en entier** — la lettre a pu changer depuis, ou une paire a été modifiée — et rien n'est créé.

Une lettre qui n'est pas encore dans Athena est d'abord **versée comme document de son dossier** (ticket de téléversement, finalité `document`), puis prévisualisée et transformée à partir de là : téléversée directement comme gabarit, une lettre est versée telle quelle, jamais transformée. Les lignes de l'aperçu sont numérotées à partir de 0 (`index`), alors que ses messages en français comptent à partir de 1 (« Substitution n° 1 » est la ligne 0).

### Règles à surveiller (transformation en gabarit)

- **La recherche tient compte des majuscules.** « Jean Tremblay » dans une phrase et « JEAN TREMBLAY » dans un titre forment DEUX paires, et celle du titre prend un nom de champ EN MAJUSCULES — `{{CLIENT.NOM_COMPLET}}` —, qui imprime du même coup le nom du nouveau client en majuscules (la règle de la section 1). La vérification qui suit les remplacements, elle, ne tient PAS compte des majuscules : le titre oublié est détecté, et nommé.
- **Mots entiers seulement**, et le **texte le plus long d'abord** : « Jean Tremblay » réserve son passage avant que « Tremblay » seul ne puisse s'en emparer. Les espaces insécables et les espaces fines, les apostrophes courbes et les traits d'union Unicode (U+2010, U+2011) correspondent à leur forme simple; rien d'autre n'est assimilé.
- **Un texte que Word a fragmenté est trouvé en entier** — un prénom en gras suivi d'un nom de famille dans une autre langue de vérification forme un seul texte. C'est précisément le cas que la réparation automatique des champs fragmentés, au remplissage, ne sait pas traiter, et c'est pourquoi la transformation en gabarit ne s'y fie jamais.
- **Seuls le corps, les en-têtes et les pieds de page sont réécrits** — les parties que le remplissage remplit. Une occurrence dans une note de bas de page, une note de fin, un commentaire ou les propriétés du document est COMPTÉE et laissée en place (un champ placé là ne serait jamais rempli); de même pour une occurrence dans le résultat d'un champ Word (Word le régénère), dans un contrôle de contenu lié à des données, coupée par un trait d'union conditionnel ou insécable, ou à l'intérieur d'un `{{…}}` que la lettre contient déjà. L'aperçu les énumère toutes; retirez-les dans Word.
- **Les propriétés du document sont TOUJOURS vidées** dans la copie transformée — titre, objet, auteur, auteur de la dernière modification et description —, AVANT que les remplacements soient comptés (l'aperçu compte de la même façon; `scrub_properties: false` est refusé). La vérification ne lit que les noms, les numéros et les adresses, et un objet qui dit de quoi traitait le premier dossier n'est rien de cela — il ne figure d'ailleurs sur aucune page que vous relisez. Toute autre propriété (mots clés, société, propriétés personnalisées) est seulement vérifiée : un nom qui y resterait s'efface dans Word (Fichier › Informations › Propriétés).
- **Un texte plus long qui ne remplace rien réserve quand même son passage.** « Marie‑Ève Tremblay », écrit avec le trait d'union insécable de Word, est trouvé mais jamais remplacé (`blocked_by_markup`), et « Tremblay » à l'intérieur n'est pas compté tant que le texte plus long figure dans la liste. `create_template` ne peut pas recevoir une paire dont le nombre est 0; l'aperçu indique donc ce que « Tremblay » trouverait sans le texte plus long : retirez la paire qui ne remplace rien et relancez l'aperçu.
- **Fichiers refusés :** les modifications suivies (acceptez-les ou refusez-les d'abord dans Word), les commentaires (supprimez-les), un fichier « Strict Open XML » (enregistrez-le comme « Document Word (.docx) » ordinaire), un fichier mal formé ou trop volumineux.
- **Ce qui subsiste est refusé.** Le résultat — et non la lettre — est comparé aux parties, aux numéros et aux adresses du dossier d'origine, ainsi qu'à chaque texte dont le remplacement a été demandé; un reste entraîne le refus, à moins que vous ne l'acceptiez nommément (`accept_residual`). Une seule exception, déclarée : un texte dont tous les mots se trouvent dans le NOM d'un champ du résultat (« Nom complet » remplacé par `{{client.nom_complet}}`) n'est pas vérifié — la vérification lit du texte, et signalerait sinon le champ même qui l'a remplacé.
- **La vérification lit les noms, les numéros et les adresses — jamais le reste de la lettre.** Une phrase qui raconte l'histoire du premier client y est toujours : relisez le nouveau gabarit avant de vous y fier.
- **La lettre elle-même n'est jamais modifiée** : le gabarit est un nouveau fichier, sous un nom de fichier neutre (le nom du gabarit, jamais celui de la lettre), qui garde la trace du dossier d'où il vient.

---

## En bref

- Les noms de personnes s'impriment **sans civilité par défaut**; employez la variante `…_avec_civilite` pour obtenir la civilité (dans le bloc d'adresse d'une lettre, pas dans l'intitulé d'un acte de procédure).
- Les noms de champs sont reconnus **sans égard aux majuscules**; un nom EN MAJUSCULES **met la valeur en majuscules**.
- Les champs inconnus sont **sans risque** : ils restent tels quels, et la génération n'échoue jamais.
- Champ automatique vide → `[CHAMP MANQUANT : …]`; champ à saisir vide → `[À COMPLÉTER : …]`; champ laissé tel quel → `{{name}}` brut.
- La transformation d'une lettre en gabarit (voir la section 9) **tient compte des majuscules**, ne remplace que des mots entiers, se fait en tout ou rien, et est refusée tant qu'il reste quoi que ce soit du dossier d'origine.
