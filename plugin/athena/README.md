# Athena — plugin 2.0.2

Une compétence qui apprend à Claude à se servir du connecteur Athena **avec le moins
d'appels possible** : routage clair, suite d'appels minimale par demande courante,
confirmation graduée et cloisonnement du *secret professionnel* face aux appels
réseau sortants (connecteurs de `droit-civil-quebecois` et recherche web).

- `skills/athena/SKILL.md` — routeur unique + `SAFETY-KERNEL` et quatre recettes (`R1`–`R4`) ; ≤ 1 000 mots.
- `skills/athena/references/*.md` — six feuilles terminales (dossiers/contacts, agenda/protocole, notes/documents, facturation/budget, comptabilité/états, reprise), ≤ 1 500 mots chacune.
- `skills/athena/references/index-outils.md` — **généré** ; lu seulement quand aucune recette ne convient.
- `.mcp.json` — désigne le connecteur **par son nom, « Athena »**, jamais par une URL : dans
  Claude Code, une URL masquerait le connecteur claude.ai sans pouvoir s'authentifier. Le
  plugin ne crée donc pas le connecteur : il s'ajoute une fois dans claude.ai.

## Réglages par surface

- **claude.ai** : le connecteur se nomme **« Athena »** (`https://athena.poirierlavoie.ca/mcp`),
  en mode « Sur demande » ; « Exécution de code et création de fichiers » activée, sans quoi
  aucune compétence ne se charge. Envisager « Bloqué » pour `preview_templatize`,
  `create_template` et `import_invoice` — à débloquer le temps d'une gabarisation ou d'une reprise.
- **Remplace `pallas-athena`** : retirer cet ancien plugin (bibliothèque de l'organisation et
  « Mes téléversements »), sinon ses copies restent chargées, Claude Code compris.
- **Complément Excel et Toolbox** : désactiver le connecteur lorsque non requis pour éviter les
  chargements d'outils inutiles.

## Reconstruire et livrer

La source vit ici, hors d'`athena/` : elle n'est jamais déployée. Les lignes `{{GEN:…}}` se
remplissent à la construction ; ne jamais les écrire à la main.

```bash
cd athena
python -m pytest tests/test_plugin_athena.py -q   # la porte, d'abord
python -m scripts.exporter_plugin_athena           # → ../dist/athena-2.0.2.plugin (non versionné)
rm -rf ../../athena-plugin/plugins/athena
python -m scripts.exporter_plugin_athena --deplie ../../athena-plugin/plugins/athena
```

Puis, dans ce miroir (`jpoirierlavoie/athena-plugin`) : reporter la version `2.0.2` dans
`.claude-plugin/marketplace.json`, valider, pousser. claude.ai s'y synchronise (réglage de
l'organisation), puis Claude Code : rien à téléverser.

## Ce qui le protège

`athena/tests/test_plugin_athena.py`, dans la porte de déploiement, échoue quand une citation
se périme (outil/paramètre renommé ou retiré, valeur d'énumération absente), qu'un budget de
mots ou d'octets casse, qu'un lien croisé `../` ou entre feuilles apparaît, ou qu'un scénario
d'évaluation hors ligne échoue ; `--check` vérifie que l'archive de `dist/` correspond exactement à
la reconstruction courante.
