# Accounting, Billing & Financial Statements

This document is the thematic specification for billing, invoicing, Québec sales taxes, trust and administration accounting registers, bank reconciliations, write-offs, and financial statements in Pallas Athena. It complements [CLAUDE.md](CLAUDE.md) / [GEMINI.md](GEMINI.md), [LITIGATION.md](LITIGATION.md), [SECURITY.md](SECURITY.md), and [DEPLOYMENT.md](DEPLOYMENT.md).

Authoritative implementations live in `models/invoice.py`, `models/admin_ledger.py`, `models/fee_payment.py`, `models/releve_conciliation.py`, `models/financial_statements.py`, `services/comptabilite.py`, `services/financial_statements.py`, `services/note_credit.py`, and `utils/format_fr.py`.

## Foundational principles

- **Money in integer cents.** `15000` is 150,00 $, never a float. `Decimal` serves only intermediates (tax computations, parsing typed amounts), rounded `ROUND_HALF_UP` to the cent before storage. Parse and display money exclusively through `utils/format_fr`.
- **Strict fund separation.** Client trust funds (`fidéicommis`) and firm administration funds (`administration`) are parallel and must never be merged or totalled together.
- **Permanent audit trail.** Trust entries are append-only; write-offs, credit note numbers, sequence numbers (`counters/{scope}` inside the writing transaction, never reused), and completed reconciliations are immutable or corrected solely by dated reversal events.
- **Single authority per computation.** Never duplicate balance, tax, overdue, classification, or financial-statement math in routes, templates, or scripts; call the owning model or service authority.

## Invoices, taxes & write-offs (`models/invoice.py`, `services/note_credit.py`)

- **Taxes (GST / QST):** TPS (5 %) and TVQ (9.975 %) apply to the same taxable base and are never compounded. Stored rates use distinct scales (`gst_rate` ×100, `qst_rate` ×1000).
- **Time entries & expenses:** A time entry or expense sits on at most one live invoice. Once drawn onto an invoice, it is frozen except for its phase; only voiding (`annulée`) the invoice releases it.
- **Numbering:** Generated invoice numbers (`YYYY-F###`, per Montréal calendar year) come from `counters/invoices-{year}` inside the writing transaction. Imported invoice numbers keep their existing form and never touch the counter.
- **Invoice dates & due dates:** Invoice dates are stored at UTC midnight. `plan_invoice` (the web form and `import_invoice`) refuses a date after the current UTC day and accepts that UTC day itself (ahead of Montréal each evening); the MCP connector's `create_invoice` and `preview_invoice` keep their stricter Montréal-day check by decision (`mcp/handlers._invoice_date`). The due date defaults to the invoice date plus one calendar month (`invoice.default_due_date`).
- **Overdue status (`en_retard`):** Lateness follows the due date automatically, prorogued to the next business day on the civil calendar. `invoice.is_overdue` is the single authority, and a daily sweep at 9 h (Montréal) marks overdue invoices behind `FACTURES_RETARD_ACTIF`.
- **Lifecycle & payments:**
  - Only a `brouillon` invoice may be corrected; `amount_due` is frozen at issuance.
  - « envoyée » is a status marker only and sends nothing.
  - « payée » follows the accounting registers and write-offs—never a manual status toggle.
  - Payments exist solely as register entries (an administration encaissement or a trust fee payment).
- **Write-offs (« radiation ») & credit notes:**
  - A write-off is a dated event stored on the invoice (`write_offs`), never a register entry. It settles an overdue invoice's entire remainder (`invoice.balance_of` = due + interest − paid − written off) and is cancelled only by a reversal dated that day. Tax relief (`invoice.write_off_tax_parts`) applies strictly to the unpaid invoiced principal (fees and expenses), never to tax-exempt overdue interest.
  - Toward the client, a write-off is a price reduction documented by a credit note numbered `AAAA-NC###` from `counters/notes-credit-{year}` (Montréal year) inside the write-off transaction. A cancelled radiation's credit note keeps its number with status « annulée ».
  - The credit note Word document (`.docx`) is generated on demand from a standing radiation (`services/note_credit.py`) and filed in « Mandat › Factures » just like the note d'honoraires; a cancelled radiation produces no document.
  - In the books, a write-off is recorded as a bad debt.
  - Any payment on an invoice carrying a standing radiation is refused in both registers until the radiation is cancelled.
- **Overdue interest notices (« Avis d'intérêts »):**
  - Strictly manual on overdue (`en_retard`) invoices with an unpaid principal (`unpaid_principal_of > 0`) and no standing radiation (never an automatic cron). Simple annual interest (`Actual/365`, `ROUND_HALF_UP`) accrues on unpaid principal from `due_date` (or prior standing notice `period_end`) through the chosen `notice_date` at the cabinet annual rate configured in `Paramètres → Profil du cabinet`, **exempt from TPS/TVQ**. Partial payments impute chronologically (`notice.date <= payment.date`) to standing interest first, then principal (art. 1571 C.c.Q.), so subsequent interest notices never retroactively shift extinguished capital back into unpaid principal.
  - Each notice mints `AAAA-AI###` from `counters/avis-interets-{year}` transactionally, appends to `invoice.interest_charges`, supports dated reversal (`contre_passation`), and generates `.docx` via `services/avis_interets.py` using the active `avis_interets` template. Voiding an invoice is refused while an interest notice stands.

## Trust & administration registers (`models/admin_ledger.py`, `models/fee_payment.py`, `services/comptabilite.py`)

- **Shared orchestration:** Both the web UI and the MCP connector create, pay, reverse, clear, and edit register entries through `services/comptabilite.py`.
- **Trust register (client funds):** Append-only and sequence-ordered. An entry changes only through its clearing lifecycle, is corrected solely by a reversal dated today, and overdraft is checked against the cleared balance inside the writing transaction.
- **Administration register (firm funds):** Entries remain editable only while no completed reconciliation locks their period.
- **Non-operating & special entry kinds:**
  - Lawyer drawings and contributions (`prélèvement`, `apport`) and transfers between firm administration accounts or outside the register (`virement_interne_sortant`, `virement_interne_entrant`, `paiement_carte`) never enter revenue, expenses, or TPS/TVQ totals (`admin_ledger.results_statement`). Two-leg firm transfers (any two active `admin_accounts`, including `paiement_carte`) commit atomically in one transaction with **both legs born `status="compensée"` immediately** at `cleared_date = date`.
  - Firm TPS/TVQ remittances and refunds (`remise_tps_tvq`, `remboursement_tps_tvq`, one combined amount) settle the firm's tax balance; no revenue, expense, or tax-credit total counts them either.
  - Neither tax remittances/refunds nor the lawyer's own drawings/contributions carry a dossier (`admin_ledger.NO_DOSSIER_KINDS`).
  - A client advance received in the operations account instead of in trust (`avance_honoraires`, named as in the trust register) is client money held as a liability outside operating results until a `virement_interne_sortant` marked « avances » (`admin_ledger.TRANSFER_PURPOSES`) moves it to trust. The dossier + client-linked **Operations → Trust client-advance bridge** (`models/advance_transfer.py`) writes both the outgoing `virement_interne_sortant` (`transfer_purpose="avances"`) and the Trust `avance_honoraires` deposit atomically, **both born `compensée` immediately**, checking reconciliation locks on both registers and reversing atomically while guarding against trust shortfalls (art. 59).
  - A supplier expense refund (`remboursement_depense`) carries a category and tax split and nets against expenses, never counting as revenue (`admin_ledger.REFUND_KINDS`).
- **Fee payments:** A fee payment commits the trust withdrawal, the administration receipt, and the invoice payment atomically in one transaction (`models/fee_payment.py`).
- **Bank reconciliations & statements:** A reconciliation completes only with the supporting bank statement it rests on (art. 32: one file, attached, replaced, or removed while « brouillon », frozen once « complétée »; a completed reconciliation lacking it accepts it once through « Relevés manquants »). `models/releve_conciliation` is the single door to that file, which the MCP connector never reaches.

## Financial statements & write-off journal (`models/financial_statements.py`, `services/financial_statements.py`)

- **Single authority:** `models/financial_statements` is the sole authority for every figure, label, and row key of the statements page (`/finances/`), its PDF export, and the MCP connector's `get_financial_statements`, which serves rows for a series of periods (month, quarter, or calendar year) computed from one read-only snapshot (`services/financial_statements`).
- **Revenue:** Billed revenue—invoices in `invoice.REVENUE_STATUSES` (`envoyée`, `en_retard`, `payée`), attributed to the period of their invoice date, before tax, plus net overdue interest (`avis_interets` less reversals) at the notice date. A write-off takes nothing off revenue; an `annulée` invoice counts in no period.
- **Costs & bad debts:** Operating costs are expenses net of TPS/TVQ less expense refunds, classed by `admin_ledger.cost_class`, plus bad debts. Each write-off enters net of the TPS/TVQ it claims as an expense « Créances irrécouvrables » under operating expenses (with variable costs, below gross margin) at its own date; a cancellation counts back at its date, and radiations group under « Redressements » (one row per event).
- **Balance sheet & tax:** TPS/TVQ lives exclusively on the balance sheet (billed tax less CTI/RTI, remittances, and the TPS/TVQ claimed on write-offs, plus refunds); overdue interest is exempt from TPS/TVQ and adds to receivables (`world.balance`) and net income directly. Client advances not yet moved to trust are a liability, and neither they nor their marked transfer to trust alter net assets.
- **Ledger resolution & memos:**
  - Corrections resolve over the whole ledger before any period slicing (`admin_ledger.resolve_corrections`).
  - A trust-linked « autre recette » counts toward an invoice only via its trust entry's `invoice_id` or a unique normalized match in the same dossier (`invoice.normalize_reference`).
  - A hand-set « payée » shortfall is presumed settled and listed.
  - Unbilled work and trust funds are shown as memos only, never inside totals.
  - Books start on `BOOKS_START` with no opening balance.
- **Fail-closed snapshots & rollforward integrity:** A failed or truncated collection marks every section that depends on it « indisponible ». The equity rollforward's « écart non expliqué » is zero by construction: every discrepancy is named or measured as a defect and listed under « points à vérifier ».
- **Write-off & interest journal:** The « Journal des radiations et intérêts » (`/comptabilite/radiations/`, linked from the Comptabilité hub) stores nothing on its own; `financial_statements.write_off_journal` derives the period's write-off events and overdue-interest notices (`INTEREST_JOURNAL_COLUMNS`) from that same snapshot slice, surfaced across HTML, CSV, and PDF exports so totals reconcile directly to the financial statements.
