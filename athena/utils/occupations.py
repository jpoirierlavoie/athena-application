"""A natural person's occupation — the ONE vocabulary (2026-10-30).

The *Règlement sur la comptabilité et les normes d'exercice professionnel
des avocats* (RLRQ, c. B-1, r. 5, art. 14 al. 1 (1) d)) requires the lawyer
to record a client's « occupation ». The status lives here; what the person
does (« Poste ou profession », vCard TITLE) and where (the organization and
the work coordinates on the contact) stay in their existing fields.

Pure — standard library only — because the client portal imports it too
(``athena/client/`` may import ``utils``, never ``models``): the contact
model, the connector's enum, the contact form, Réception and the portal's
intake form read this one list, and tests pin each copy to it.

No vCard property exists for it (RFC 6350, RFC 6715, RFC 9554 and JSContact
have none): it is never serialized, like the compliance checks.
"""

from __future__ import annotations

# key → French label, in the order the forms offer them.
OCCUPATION_LABELS: dict[str, str] = {
    "salarie": "Salarié(e)",
    "travailleur_autonome": "Travailleur(euse) autonome / entrepreneur(e)",
    "etudiant": "Étudiant(e)",
    "retraite": "Retraité(e)",
    "sans_emploi": "Sans emploi",
    "benevole": "Bénévole",
    "au_foyer": "Au foyer",
    "autre": "Autre",
}
OCCUPATIONS: tuple[str, ...] = tuple(OCCUPATION_LABELS)

# What the contact's ``organization`` field names, by occupation.
ORGANIZATION_LABELS: dict[str, str] = {
    "salarie": "Employeur",
    "travailleur_autonome": "Entreprise",
    "etudiant": "Établissement d'enseignement",
    "benevole": "Organisme",
}
ORGANIZATION_LABEL_DEFAULT = "Employeur ou organisation"

# Occupations with no current employer: the portal hides (and blanks) its
# employer block for them.
WITHOUT_EMPLOYER: frozenset[str] = frozenset({"retraite", "sans_emploi", "au_foyer"})


def occupation_label(occupation: str) -> str:
    """The label of *occupation*, ``""`` when unset or unknown."""
    return OCCUPATION_LABELS.get(occupation or "", "")


def organization_label(occupation: str) -> str:
    """What to call the ``organization`` field for *occupation*."""
    return ORGANIZATION_LABELS.get(occupation or "", ORGANIZATION_LABEL_DEFAULT)
