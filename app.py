"""AI Data Catalog: point it at a schema, get column descriptions, business
glossary terms, and PII flags back.

Architecture: introspect the schema + a few sample rows -> redact anything
that looks like PII *locally, before it ever leaves the machine* -> send
only the redacted samples to Claude for descriptions/business terms/PII
judgment -> merge Claude's opinion with the local (deterministic) PII flags.

The core guardrail: local regex/keyword detection is the privacy FLOOR. The
LLM can add PII flags (semantic judgment: "full_name" is obviously personal
even though it matches no regex), but it can never remove a flag the local
detector already set. Same shape as the NL-to-SQL project's guardrail: don't
trust the model alone for anything safety-critical — deterministic code sets
the floor, the model only adds coverage on top.
"""

import json
import os
import random
import re
import sqlite3
import sys

from anthropic import Anthropic

DB_PATH = os.path.join(os.path.dirname(__file__), "company.db")
MODEL = "claude-sonnet-5"
SAMPLE_LIMIT = 3  # data-minimization: cap how many raw rows we ever touch

# --- Layer 1: local, deterministic PII detection (runs before any API call) ---

PII_NAME_HINTS = (
    "ssn", "social_security", "email", "phone", "address", "name",
    "salary", "card", "credit_card", "dob", "birth", "passport",
)

PII_VALUE_PATTERNS = {
    "email": re.compile(r"^[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}$"),
    "ssn": re.compile(r"^\d{3}-\d{2}-\d{4}$"),
    "credit_card": re.compile(r"^\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}$"),
    "phone": re.compile(r"^\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}$"),
}


def name_suggests_pii(column_name: str) -> bool:
    lowered = column_name.lower()
    return any(hint in lowered for hint in PII_NAME_HINTS)


def detect_value_pattern(value: str):
    for pattern_name, regex in PII_VALUE_PATTERNS.items():
        if regex.match(value):
            return pattern_name
    return None


def mask_value(value: str, pattern: str) -> str:
    digits = re.sub(r"\D", "", value)
    if pattern == "email":
        local, _, domain = value.partition("@")
        return f"{local[0]}***@{domain}" if local else f"***@{domain}"
    if pattern == "phone":
        return f"***-***-{digits[-4:]}" if len(digits) >= 4 else "***"
    if pattern == "ssn":
        return f"***-**-{digits[-4:]}" if len(digits) >= 4 else "***"
    if pattern == "credit_card":
        return f"**** **** **** {digits[-4:]}" if len(digits) >= 4 else "****"
    return value


def build_masked_samples(column_name: str, sample_values: list) -> tuple[list, bool]:
    """Returns (masked sample strings safe to send to the API, local PII flag)."""
    local_flag = name_suggests_pii(column_name)
    masked = []
    for raw in sample_values:
        s = str(raw)
        pattern = detect_value_pattern(s)
        if pattern:
            local_flag = True
            masked.append(mask_value(s, pattern))
        elif name_suggests_pii(column_name):
            masked.append(f"[REDACTED · {len(s)} chars]")
        else:
            masked.append(s[:40] + ("..." if len(s) > 40 else ""))
    return masked, local_flag


# --- Demo database ---------------------------------------------------------


def init_db(db_path: str = DB_PATH) -> None:
    if os.path.exists(db_path):
        return

    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE customers (
            id INTEGER PRIMARY KEY,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL,
            phone TEXT NOT NULL,
            ssn TEXT NOT NULL,
            address TEXT NOT NULL,
            signup_date TEXT NOT NULL
        );

        CREATE TABLE employees (
            id INTEGER PRIMARY KEY,
            full_name TEXT NOT NULL,
            email TEXT NOT NULL,
            salary INTEGER NOT NULL,
            department TEXT NOT NULL,
            hire_date TEXT NOT NULL
        );

        CREATE TABLE transactions (
            id INTEGER PRIMARY KEY,
            customer_id INTEGER NOT NULL REFERENCES customers(id),
            amount REAL NOT NULL,
            card_number TEXT NOT NULL,
            transaction_date TEXT NOT NULL
        );
        """
    )

    rng = random.Random(42)

    customers = []
    for i in range(1, 11):
        ssn = f"{rng.randint(100, 999)}-{rng.randint(10, 99)}-{rng.randint(1000, 9999)}"
        phone = f"{rng.randint(200, 999)}-{rng.randint(200, 999)}-{rng.randint(1000, 9999)}"
        customers.append((
            i, f"Customer {i}", f"customer{i}@example.com", phone, ssn,
            f"{rng.randint(100, 999)} Main St, City {i}",
            f"2025-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
        ))
    conn.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?, ?)", customers)

    departments = ["Engineering", "Sales", "Support", "Finance"]
    employees = [
        (i, f"Employee {i}", f"employee{i}@company.com", rng.randint(45000, 150000),
         rng.choice(departments), f"2024-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}")
        for i in range(1, 7)
    ]
    conn.executemany("INSERT INTO employees VALUES (?, ?, ?, ?, ?, ?)", employees)

    transactions = []
    for i in range(1, 16):
        card = f"{rng.randint(4000, 4999)} {rng.randint(1000, 9999)} {rng.randint(1000, 9999)} {rng.randint(1000, 9999)}"
        transactions.append((
            i, rng.randint(1, 10), round(rng.uniform(10, 500), 2), card,
            f"2026-{rng.randint(1, 9):02d}-{rng.randint(1, 28):02d}",
        ))
    conn.executemany("INSERT INTO transactions VALUES (?, ?, ?, ?, ?)", transactions)

    conn.commit()
    conn.close()


def get_schema(db_path: str = DB_PATH) -> dict:
    """table -> [(column_name, sql_type), ...]"""
    conn = sqlite3.connect(db_path)
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    schema = {}
    for table in tables:
        schema[table] = [(c[1], c[2]) for c in conn.execute(f"PRAGMA table_info({table})")]
    conn.close()
    return schema


def get_sample_rows(table: str, columns: list, db_path: str = DB_PATH, limit: int = SAMPLE_LIMIT):
    """column_name -> [raw sample values] (raw — masking happens by the caller)."""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(f"SELECT * FROM {table} LIMIT {limit}").fetchall()
    conn.close()
    by_column = {name: [] for name, _ in columns}
    for row in rows:
        for (name, _), value in zip(columns, row):
            by_column[name].append(value)
    return by_column


# --- Layer 2: LLM description / business-term / PII-judgment -----------------


def generate_catalog_entries(client: Anthropic, table: str, column_infos: list) -> list:
    """column_infos: [{"name", "type", "masked_samples", "local_pii_flag"}, ...]
    Returns Claude's per-column opinion (never trusted alone for PII — see merge step)."""
    schema = {
        "type": "object",
        "properties": {
            "columns": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "description": {"type": "string"},
                        "business_term": {"type": "string"},
                        "pii_likely": {"type": "boolean"},
                        "pii_reason": {"type": "string"},
                    },
                    "required": ["name", "description", "business_term", "pii_likely", "pii_reason"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["columns"],
        "additionalProperties": False,
    }

    lines = [
        f'- {c["name"]} ({c["type"]}): sample values = '
        f'[{", ".join(c["masked_samples"]) or "(no sample data)"}]'
        for c in column_infos
    ]
    user_content = (
        f"Table: {table}\n"
        f"Columns:\n" + "\n".join(lines) + "\n\n"
        "For each column: write a one-sentence business-friendly description, "
        "suggest a business glossary term, and judge whether the column is "
        "likely to contain PII based on its name, type, and sample values. "
        "Sample values may already be partially redacted for privacy — that's "
        "intentional. Use the visible signal (format, length, column name) "
        "rather than assuming a redacted value means 'not PII'."
    )

    response = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": user_content}],
    )
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)["columns"]


def build_table_catalog(client: Anthropic, table: str, columns: list) -> list:
    """The merge step: local PII flags are a floor the LLM can never lower."""
    raw_samples = get_sample_rows(table, columns)
    column_infos = []
    for name, sql_type in columns:
        masked, local_flag = build_masked_samples(name, raw_samples[name])
        column_infos.append({
            "name": name, "type": sql_type,
            "masked_samples": masked, "local_pii_flag": local_flag,
        })

    llm_entries = {e["name"]: e for e in generate_catalog_entries(client, table, column_infos)}

    catalog = []
    for col in column_infos:
        llm = llm_entries.get(col["name"], {})
        final_pii = col["local_pii_flag"] or llm.get("pii_likely", False)
        reason_parts = []
        if col["local_pii_flag"]:
            reason_parts.append("matched locally by column name or value pattern")
        if llm.get("pii_reason"):
            reason_parts.append(llm["pii_reason"])
        catalog.append({
            "name": col["name"],
            "type": col["type"],
            "description": llm.get("description", "(no description generated)"),
            "business_term": llm.get("business_term", ""),
            "pii": final_pii,
            "pii_reason": "; ".join(reason_parts) if final_pii else "",
        })
    return catalog


def print_catalog(table: str, catalog: list) -> None:
    print(f"\n=== {table} ===")
    for col in catalog:
        pii_tag = " [PII]" if col["pii"] else ""
        print(f"\n{col['name']} ({col['type']}){pii_tag}")
        print(f"  description:    {col['description']}")
        print(f"  business term:  {col['business_term']}")
        if col["pii"]:
            print(f"  pii reason:     {col['pii_reason']}")


def main() -> None:
    init_db()
    schema = get_schema()
    client = Anthropic()

    print("AI Data Catalog — generating descriptions and PII flags per table.")
    for table, columns in schema.items():
        catalog = build_table_catalog(client, table, columns)
        print_catalog(table, catalog)


if __name__ == "__main__":
    sys.exit(main())
