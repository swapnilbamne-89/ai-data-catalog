# Interview Q&A: AI Data Catalog

Prep doc for talking through this project in a PM interview. Answers are
written in first person and grounded in the actual implementation in
`app.py` — no invented metrics, no generic AI platitudes.

---

## 1. Product & scoping questions

### What problem does this solve and for whom?

Every data warehouse past a certain size accumulates tables nobody
documented — a column called `status` with no legend, a `full_name` field
next to a `name` field, nobody remembers why. The person who pays for that
is a new analyst or data-team hire who has to either guess or interrupt
someone senior to ask. This tool points at a schema, pulls a few sample rows
per table, and generates a plain-English description, a business glossary
term, and a PII flag for every column so that inheriting an undocumented
warehouse doesn't start with archaeology.

### How would you scope MVP vs. V2?

What's built is intentionally narrow: single SQLite file, read schema via
`PRAGMA table_info`, sample up to 3 rows (`SAMPLE_LIMIT`), generate three
fields per column, print to stdout. That's the whole MVP — no persistence,
no UI, no auth. V2 is everything the README lists as out of scope:
versioning/diffing the catalog as schemas drift, a human review/approval
workflow before anything is treated as authoritative, free-text PII
detection, and a real NER library instead of regex+keywords. I scoped MVP to
answer one question well — "can an LLM produce a usable first draft of
column documentation from redacted samples" — rather than trying to be a
catalog product on day one.

### Why generate a business glossary term specifically, not just a description?

A description tells you what a column *is*; a glossary term tells you how it
maps to language the business already uses — is `cust_id` the same concept
as "Customer" in the CRM, is `amt` the same as "Transaction Amount" in
finance's reporting? That mapping is what lets someone write a query or a
BI report using vocabulary a stakeholder recognizes, and it's also what lets
a data team eventually reconcile the same concept across systems that name
it differently. A description alone doesn't do that linking work — it's
useful but it doesn't reduce the "wait, is this the same field as X" problem
that a glossary term is meant to solve.

### What metrics would you track if this shipped?

The one that actually tells you if it's working: % of generated
descriptions and business terms accepted as-is by a human reviewer versus
edited or rejected, broken out by table and by whether the column was
flagged PII. A high edit rate on descriptions is a quality signal on the
LLM prompt; a high edit rate on PII flags is a much bigger deal because it
means the merge rule's floor isn't catching what reviewers think it should.
I'd also track false-negative reports specifically for PII — a human finding
a column that should've been flagged and wasn't — since that's the failure
mode with the highest cost in this design.

### Why print to stdout instead of writing directly into a real catalog tool like DataHub, Amundsen, or Atlan?

Honestly, mostly a demo shortcut, but it's a deliberate one, not an
oversight — writing into DataHub/Amundsen/Atlan means an ingestion API,
auth, and (most importantly) a decision about whether AI-generated metadata
becomes authoritative the moment it's written, versus sitting in a review
queue. Printing to stdout sidesteps that decision entirely rather than
answering it badly. If I built V2, the review/approval workflow question
would have to be answered *before* wiring up a write path, because writing
directly into a production catalog without human review is the kind of
thing that quietly erodes trust in the catalog itself once one bad
AI-generated description ships.

### How do you decide what's out of scope?

I look for the boundary where a "yes, but" answer stops being honest. Free-
text PII detection is out because the regex patterns match values that are
*entirely* a pattern (see `PII_VALUE_PATTERNS`), not PII embedded in a
sentence — claiming otherwise would be lying about what regex can do. Real
NER is out because it's a dependency and a scope decision, not a detail I
forgot. Versioning and human review are out because they're each their own
product surface (a diffing UI, an approval workflow) that would have
diluted the one thing I actually wanted to demonstrate cleanly: the local-
detection-as-floor pattern. Scoping honestly means naming the exact
mechanism that doesn't work, not just waving at "V2 has more features."

---

## 2. Technical/architecture questions

### Walk me through the request flow.

`main()` calls `init_db()` to seed `company.db` if it doesn't exist, then
`get_schema()` runs `PRAGMA table_info` per table to get column names and
types. For each table, `build_table_catalog()` runs: `get_sample_rows()`
pulls up to `SAMPLE_LIMIT` (3) raw rows per column, `build_masked_samples()`
runs both local PII checks and masks anything that matches, then
`generate_catalog_entries()` sends only the masked samples plus schema
metadata to Claude with a `json_schema` output config and gets back a
description, business term, and PII judgment per column. The merge step
computes `final_pii = local_flag or llm_flag` per column, and
`print_catalog()` writes the result to stdout. Nothing touches the network
before the mask step; nothing gets treated as final before the merge step.

### Why two independent local PII detection layers instead of one?

`name_suggests_pii()` and `detect_value_pattern()` catch different things.
A column named `ssn` gets flagged by name even if, for some reason, its
sample values didn't parse as SSN-shaped strings. A column named something
generic like `contact` or `identifier` that happens to hold an email or
phone number gets caught by the regex on the value even though the name
gives no hint. Either check alone has a gap the other one closes — name-only
misses oddly-named PII columns, value-only misses PII columns that happen to
sample empty or malformed values. Running both and OR-ing them
(`build_masked_samples` sets `local_flag = True` if either fires) is a wider
net for basically no extra cost, since both are just string operations.

### Why does the merge rule only let the LLM ADD flags and never remove them?

Because `final_pii = local_flag or llm_flag` (in `build_table_catalog`) is a
one-way ratchet by construction — there's no code path where an LLM output
can flip a `local_pii_flag=True` back to false. If it worked the other way —
if the LLM's judgment could override the local flag — then a prompt-injected
sample value, an ambiguous column name, or the model just being
overconfident that "this looks like a synthetic ID, not real PII" could
silence a real detection. That's the exact failure mode you don't want in a
privacy-adjacent feature: a plausible-sounding LLM explanation that's wrong
being trusted more than deterministic pattern matching that was right. The
ratchet means the worst the LLM can do on PII is over-flag, never under-flag
something the regex already caught.

### Why cap sample rows at 3 instead of more?

Two reasons, and they're linked. First, data minimization — the fewer raw
rows the local masking step ever has its hands on, the smaller the blast
radius if masking has a bug, since even in that worst case 3 rows per table
isn't a meaningful data export. Second, 3 rows is already enough signal for
the model to see a value's shape and format (is this a date string, a short
code, a long free-text field) without needing statistical coverage — the
task is "describe what kind of thing this column is," not "characterize the
distribution of values," so more rows wouldn't improve the description
quality, they'd just increase exposure for no product benefit.

### Why structured JSON output instead of free text for this task specifically?

Free text works fine when a human is going to read one answer and act on
it once, which is closer to the sibling SQL project's shape. Here I need
three distinct fields — description, business_term, pii_likely, plus a
reason — per column, for potentially dozens of columns across multiple
tables, and every one of those fields feeds downstream logic: `pii_likely`
specifically gets OR'd into a boolean in the merge step. Parsing that
reliably out of free text ("is 'unlikely' a false or an unclear-should-flag
signal?") is fragile in a way that matters more here because the PII
boolean is safety-relevant, not just a nice-to-have. The `output_config`
json_schema with `additionalProperties: False` and required fields
(`generate_catalog_entries`) makes the model return a boolean I can rely on
directly, not a phrase I have to interpret.

### What happens if Claude's JSON response is missing a column that was in the request?

`build_table_catalog` builds `llm_entries` as a dict keyed by column name
from whatever came back, then for each column in `column_infos` it does
`llm_entries.get(col["name"], {})` — a missing column just resolves to an
empty dict. The description falls back to `"(no description generated)"`,
business_term to `""`, and critically `pii_likely` defaults to `False` via
`llm.get("pii_likely", False)`. That's safe specifically because of the
merge rule: if the local detector already flagged that column,
`final_pii = local_flag or False` still evaluates to `True`. The LLM
dropping a column silently degrades the description quality for that
column, but it can't silently un-flag a real local PII detection — that's
the ratchet doing its job even in a partial-failure case I didn't
explicitly write a branch for.

### Why redact-then-send instead of describe-without-samples — why not just send column names and types with zero sample data?

I considered it, but sample data is what makes the description and glossary
term actually useful rather than a guess from the column name alone — a
column named `status` with sample values `active`/`churned`/`trial` gets a
meaningfully better description than `status (TEXT)` with nothing else.
Zero-sample-data is the safest possible option but it downgrades the
product to "restate the schema in nicer words," which isn't worth the LLM
call. The masking step is what lets me keep the samples without keeping the
risk — `build_masked_samples` replaces anything PII-shaped with a masked
form (`***-**-1409`) or a length-only stub (`[REDACTED · 11 chars]`) before
it's ever placed in the prompt, so the model still sees shape and format
without seeing the actual value.

---

## 3. Risk, safety, and failure-mode questions

### What's your biggest privacy risk here and how do you mitigate it?

The biggest risk is sample data — real customer PII — reaching a third-party
API in a form that's identifiable. I mitigate it with the two-layer local
detection (`name_suggests_pii` on column names, `detect_value_pattern` with
regex on actual values) running entirely before the network call, so masking
happens in `build_masked_samples` and only the masked output is what
`generate_catalog_entries` ever puts in a prompt. The remaining risk after
that is what the regex doesn't catch, which is exactly why the README is
explicit about the free-text gap rather than implying the detector is
complete.

### Walk me through why sending sample data to a third-party LLM API is inherently risky, and how the local-masking-first design addresses that.

Any time data leaves your infrastructure it's subject to a different
retention policy, a different threat model, and a provider you don't fully
control — even with a reputable vendor, "we sent it" is a fact you can't
un-send. The design addresses that by treating "before the API call" as the
only trust boundary that matters: `build_masked_samples` runs synchronously,
locally, with zero dependencies beyond the stdlib `re` module, before
`generate_catalog_entries` is ever invoked. There's no code path where raw
sample values reach the `Anthropic` client — the masking isn't a
post-processing step on the response, it's a precondition on the request.
That's the same shape as the sibling NL-to-SQL project's read-only DB
connection: the safety mechanism is deterministic code sitting in front of
the model call, not something the model is asked to self-police.

### What's a category of PII this system would completely miss?

Free-text PII — an email address, phone number, or full name typed into a
`notes` or `comments` field as part of a sentence, not as the entire column
value. `detect_value_pattern` uses `re.match` against patterns anchored with
`^`/`$`, so they only match when the *entire* value is the pattern; a value
like `"customer emailed me at j.smith@example.com about a refund"` matches
none of the four patterns and isn't caught by `name_suggests_pii` either
unless the column name itself contains a hint like "email." I'm being
explicit about this rather than hiding it because it's the single largest
gap in the system as built — a production version handling real free-text
fields would need actual NER, not regex, and I'd rather say that plainly
than let a demo with clean SSN/email/phone/card columns imply more coverage
than exists.

### How would you evaluate whether the PII detection is "good enough" to trust in production? What would a false negative cost versus a false positive?

I'd want a labeled sample of real (or realistic) columns run through both
layers, scored for precision and recall against a human-reviewed ground
truth, tracked separately from the LLM's opinion so I could see how much
work the local floor is doing versus the LLM's additive layer. The costs
are asymmetric: a false negative — PII that ships unflagged into a catalog
someone treats as authoritative — is the expensive failure, potentially a
compliance incident depending on the data; a false positive just means a
column gets an unnecessary `[PII]` tag and a human wastes a few seconds
confirming it's not sensitive. That asymmetry is exactly why the merge rule
is a ratchet that only adds flags — it's deliberately biased toward more
false positives if that's the tradeoff against fewer false negatives.

### What would you do differently for a real production data catalog handling actual customer data, not synthetic data?

First, add a real PII/NER library like Presidio as a **second stage** —
not a replacement for the regex layer. I checked how dedicated PII-redaction
tooling actually structures this: regex/checksum checks stay first because
they're sub-millisecond and catch structured PII deterministically; NER
only runs after, for the free-text cases regex can't reach, because it's
meaningfully slower. Keeping the fast deterministic pass as the floor and
adding NER on top is the same shape as the local/LLM split this app already
has, just one layer deeper. That's the gap I already know about and named
in the README rather than discovering it in production. Second, add the
human review/approval
workflow that's explicitly missing now — nothing should become authoritative
catalog metadata (especially a PII flag) without a reviewer confirming it,
because right now the tool prints to stdout and nothing downstream treats it
as final, which is fine for a demo and not fine for production. Third, I'd
want audit logging of exactly what was sent to the API per run, since
"prove nothing unmasked left the building" is a claim you want evidence for,
not just correct code.

### Couldn't someone just turn off the local detector and trust the LLM entirely for simplicity?

Technically, sure — delete `build_masked_samples` and send raw samples
straight to `generate_catalog_entries`, and you'd get a simpler diff. I'd
push back on that the same way I'd push back on the sibling project's
"why not let the LLM write to the database directly" question: an LLM's PII
judgment is a probabilistic opinion, and a probabilistic opinion is the
wrong thing to put in the one spot in this pipeline where a mistake means
raw PII already left the machine before anyone could catch it. The local
detector isn't there because I don't trust Claude's judgment on what counts
as PII — the LLM output is clearly useful, it's *additive* in the merge rule
for a reason. It's there because "detect before send" has to be
deterministic, or the whole guarantee this project is built around
("nothing unmasked reaches the API") stops being a guarantee and becomes a
hope. That's the thread connecting both projects: don't trust the model
alone for anything safety-critical, let deterministic code set the floor,
let the model add value on top of it.
