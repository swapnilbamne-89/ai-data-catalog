# AI Data Catalog

Point it at a database schema, get back column descriptions, business
glossary terms, and PII flags — automatically. Built, like its sibling
project `nl-to-sql-copilot`, to demonstrate product judgment around AI
safety as much as the AI integration itself.

## The problem

Data warehouses accumulate tables nobody documented. New hires and analysts
either guess what a column means or ping someone who's also guessing.
Writing that documentation by hand doesn't scale and immediately goes stale.
An LLM can generate plausible descriptions — the product risk is what
happens to the *sample data* you'd need to send it to make those
descriptions accurate.

## How it works

1. Introspect the schema (`PRAGMA table_info`) and pull a handful of sample
   rows per table (capped at 3 — data minimization, not just privacy theater).
2. **Before any of that data leaves the machine**, run it through a local,
   deterministic PII detector (column-name keywords + regex value patterns
   for email/phone/SSN/card numbers) and mask anything that matches.
3. Send only the masked samples + schema metadata to Claude, which returns
   a description, a suggested business glossary term, and its own PII
   judgment for each column — as structured JSON, not free text.
4. Merge the two: **the local flag is a floor Claude's opinion can never
   lower.** Claude can *add* a PII flag (it recognizes `full_name` is
   personal even with no regex behind it), but if the local detector already
   flagged a column, that flag stands regardless of what the model says.

## Guardrails (the actual point of this project)

- **Local PII detection runs before the API call, not after.** Raw SSNs,
  emails, phone numbers, and card numbers are masked (e.g. `***-**-1409`,
  `c***@example.com`) by regex — deterministic code, not a model — before
  they're ever placed in a prompt. This is the same design principle as the
  NL-to-SQL project's read-only DB connection: don't trust the model alone
  for anything safety-critical, let deterministic code set the floor.
- **The LLM can only add PII coverage, never remove it.** `final_pii = local_flag OR llm_flag`.
  A model that gets talked into saying "this isn't really PII" can't
  un-flag a column the regex already caught.
- **Sample size is capped at 3 rows per table** — enough for the model to
  see a value's shape, not enough to be a meaningful data export even in
  the worst case where masking somehow failed.
- **Structured output**, not free text: Claude returns JSON validated
  against a schema (`output_config.format`), so the app never has to parse
  or guess at a description buried in prose.

## What this doesn't handle (explicitly out of scope for a portfolio piece)

- Free-text PII (a `notes` or `comments` column containing an email typed
  into a sentence, or a name with no fixed format) — the local detector only
  catches values that are *entirely* an email/phone/SSN/card pattern, not
  PII embedded in longer text. This is regex + keyword heuristics on
  purpose, to keep the dependency footprint at zero — but it's a real,
  named gap, not a hand-wave: the standard production pattern (used by
  tools built specifically for pre-LLM PII redaction) is a **staged
  pipeline** — regex/checksum checks first (sub-millisecond, catches
  structured PII, never skipped), escalating to **NER** (e.g.
  [Microsoft Presidio](https://github.com/microsoft/presidio)) only for the
  unstructured cases regex can't reach — names, addresses, anything with no
  fixed shape. That's the specific V2 architecture this app's `local_flag`
  layer would slot into as the fast first stage, not a replacement for it.
- Versioning/diffing the catalog over time as the schema changes
- Human review/approval workflow before descriptions are treated as
  authoritative — this prints to stdout, it doesn't write anywhere

See `SCENARIOS.md` for the test matrix and `INTERVIEW_QA.md` for the
reasoning behind each tradeoff above.

## Running it

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=your-key-here   # or copy .env.example to .env
python app.py
```

First run seeds `company.db` (customers/employees/transactions, with
realistic-looking fake SSNs, emails, phone numbers, and card numbers so the
PII detector has something to actually catch) and prints a full catalog for
every table.
