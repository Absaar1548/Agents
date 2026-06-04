"""Idempotent Neo4j seed.

Run from /home/azureuser/temp/PoC/brd_agent/:
    .venv/bin/python -m scripts.seed_neo4j

Wipes the database and re-creates the synthetic ontology:

  Nodes:
    Stakeholder { name }         Sarah Chen, Marcus Wong, Priya Patel, Field Advisors
    BusinessUnit { name }        Private Wealth, Retail Lending, Custody
    RegulatoryDomain { name }    KYC, AML, SOC 2, GDPR
    System { name }              Okta, Azure Blob (EU), AML Vendor (Refinitiv)

  Relations:
    (Stakeholder)-[:WORKS_IN]->(BusinessUnit)
    (Stakeholder)-[:OVERSEES]->(RegulatoryDomain)
    (BusinessUnit)-[:SUBJECT_TO]->(RegulatoryDomain)
    (BusinessUnit)-[:INTEGRATES_WITH]->(System)
"""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from backend.sources.kg import Neo4jKGSource  # noqa: E402


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


# (name, label) tuples
NODES = [
    # Stakeholders
    ("Sarah Chen", "Stakeholder"),
    ("Marcus Wong", "Stakeholder"),
    ("Priya Patel", "Stakeholder"),
    ("Field Advisors", "Stakeholder"),
    # Business Units
    ("Private Wealth", "BusinessUnit"),
    ("Retail Lending", "BusinessUnit"),
    ("Custody", "BusinessUnit"),
    # Regulatory Domains
    ("KYC", "RegulatoryDomain"),
    ("AML", "RegulatoryDomain"),
    ("SOC 2", "RegulatoryDomain"),
    ("GDPR", "RegulatoryDomain"),
    # Systems
    ("Okta", "System"),
    ("Azure Blob (EU)", "System"),
    ("AML Vendor (Refinitiv)", "System"),
]

# (subject, rel_type, object) triples
EDGES = [
    # WORKS_IN
    ("Sarah Chen",     "WORKS_IN",        "Private Wealth"),
    ("Marcus Wong",    "WORKS_IN",        "Private Wealth"),
    ("Priya Patel",    "WORKS_IN",        "Retail Lending"),
    ("Field Advisors", "WORKS_IN",        "Private Wealth"),
    # OVERSEES (compliance ownership)
    ("Marcus Wong",    "OVERSEES",        "KYC"),
    ("Marcus Wong",    "OVERSEES",        "AML"),
    ("Marcus Wong",    "OVERSEES",        "SOC 2"),
    # SUBJECT_TO (BU → reg domain)
    ("Private Wealth", "SUBJECT_TO",      "KYC"),
    ("Private Wealth", "SUBJECT_TO",      "AML"),
    ("Private Wealth", "SUBJECT_TO",      "SOC 2"),
    ("Retail Lending", "SUBJECT_TO",      "KYC"),
    ("Retail Lending", "SUBJECT_TO",      "SOC 2"),
    ("Custody",        "SUBJECT_TO",      "SOC 2"),
    ("Custody",        "SUBJECT_TO",      "GDPR"),
    ("Private Wealth", "SUBJECT_TO",      "GDPR"),
    # INTEGRATES_WITH (BU → system)
    ("Private Wealth", "INTEGRATES_WITH", "Okta"),
    ("Private Wealth", "INTEGRATES_WITH", "Azure Blob (EU)"),
    ("Private Wealth", "INTEGRATES_WITH", "AML Vendor (Refinitiv)"),
    ("Retail Lending", "INTEGRATES_WITH", "Okta"),
    ("Custody",        "INTEGRATES_WITH", "Azure Blob (EU)"),
]


def main() -> None:
    src = Neo4jKGSource()
    if not src.verify():
        raise SystemExit(
            "Cannot reach Neo4j at " + src.uri +
            ". Start it via: (cd infra && docker compose up -d neo4j)"
        )

    with src._get_driver().session() as s:
        print("wiping existing graph...")
        s.run("MATCH (n) DETACH DELETE n")

        print(f"creating {len(NODES)} nodes...")
        for name, label in NODES:
            s.run(
                f"CREATE (n:{label} {{name: $name}})", name=name
            )

        print(f"creating {len(EDGES)} relationships...")
        for subj, rel, obj in EDGES:
            s.run(
                f"""
                MATCH (a {{name: $subj}}), (b {{name: $obj}})
                CREATE (a)-[:{rel}]->(b)
                """,
                subj=subj, obj=obj,
            )

        n_count = s.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        e_count = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
        print(f"seeded: {n_count} nodes, {e_count} relationships")

    # Sanity retrieval
    print("\nsanity: retrieve('Marcus Wong compliance'):")
    for h in src.retrieve(query="Marcus Wong compliance", k=3):
        print(f"  - {h.text}")

    src.close()


if __name__ == "__main__":
    main()
