"""Neo4j-backed KGSource for cross-section structured knowledge.

The ontology is small (handful of node labels, four relation types) so we
keep query logic simple:

  1. match_entities(query) — find graph nodes whose `name` substring-
     matches any whitespace-delimited word in the query (case-insensitive).
     This is the cheap, deterministic equivalent of an "entity linker"
     and is good enough for the PoC stakeholder/BU/regulatory domain set.

  2. traverse(entity_id, depth=1) — pull 1-hop neighbours and their
     relation types. We never go beyond depth=1 in the assembler today;
     `depth` left as a parameter so callers can drill deeper if needed.

  3. retrieve(query, k) — the assembler-facing convenience method that
     calls match_entities and traverse and returns RetrievedDoc-shaped
     items, one per matched entity. This keeps the assembler's source
     plumbing uniform with ChromaRetrievalSource.

The class connects lazily on first call and reuses one driver per
process — Bolt sessions are cheap; the driver is the expensive object.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

from neo4j import Driver, GraphDatabase

from backend.sources.base import RetrievedDoc


# Whitelist of node labels for matching. Keeps stray labels (e.g. Neo4j
# internal artifacts) from polluting hits.
_ENTITY_LABELS = ("Stakeholder", "BusinessUnit", "RegulatoryDomain", "System")


class Neo4jKGSource:
    name = "kg"

    def __init__(
        self,
        uri: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
    ):
        self.uri = uri or os.environ["NEO4J_URI"]
        self.user = user or os.environ["NEO4J_USER"]
        self.password = password or os.environ["NEO4J_PASSWORD"]
        self._driver: Optional[Driver] = None

    # ----- connection lifecycle -----
    def _get_driver(self) -> Driver:
        if self._driver is None:
            self._driver = GraphDatabase.driver(
                self.uri, auth=(self.user, self.password)
            )
        return self._driver

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def verify(self) -> bool:
        """Lightweight connectivity check used by main.py lifespan."""
        try:
            with self._get_driver().session() as s:
                s.run("RETURN 1 AS ok").consume()
            return True
        except Exception:
            return False

    # ----- entity matching -----
    @staticmethod
    def _tokenize(query: str) -> list[str]:
        # Split on whitespace and punctuation; drop short/stop tokens.
        toks = re.findall(r"[A-Za-z][A-Za-z0-9_-]+", query.lower())
        return [t for t in toks if len(t) >= 3]

    def match_entities(self, query: str, *, k: int = 4) -> list[dict]:
        """Return up to k entities whose `name` substring-matches a token
        in `query` (case-insensitive). Each result is
        {id, name, label, score}.
        score is a coarse "how many tokens matched" — used to order hits.
        """
        tokens = self._tokenize(query or "")
        if not tokens:
            return []
        labels_filter = " OR ".join(f"e:{lbl}" for lbl in _ENTITY_LABELS)
        cypher = f"""
        MATCH (e)
        WHERE ({labels_filter})
          AND any(t IN $tokens WHERE toLower(e.name) CONTAINS t)
        WITH e, [t IN $tokens WHERE toLower(e.name) CONTAINS t] AS matches
        RETURN elementId(e) AS id, e.name AS name, labels(e) AS labels,
               size(matches) AS score
        ORDER BY score DESC, name
        LIMIT $k
        """
        with self._get_driver().session() as s:
            res = s.run(cypher, tokens=tokens, k=k)
            return [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "label": next(
                        (l for l in r["labels"] if l in _ENTITY_LABELS), "Entity"
                    ),
                    "score": r["score"],
                }
                for r in res
            ]

    # ----- traversal -----
    def traverse(self, entity_id: str, *, depth: int = 1) -> list[dict]:
        """Return 1-hop (or `depth`-hop) neighbours of the entity.

        Each row: {rel, direction (in|out), other_name, other_label}.
        """
        # depth=1 covers all current call sites; the variable-length path
        # `(e)-[*1..$depth]-(other)` is more general but slower in Cypher.
        if depth == 1:
            cypher = """
            MATCH (e)-[r]-(other)
            WHERE elementId(e) = $entity_id
            RETURN type(r) AS rel,
                   CASE WHEN startNode(r) = e THEN 'out' ELSE 'in' END AS direction,
                   other.name AS other_name,
                   [l IN labels(other) WHERE l <> 'Entity'][0] AS other_label
            """
        else:
            cypher = """
            MATCH (e)-[*1..$depth]-(other)
            WHERE elementId(e) = $entity_id
            RETURN '...path' AS rel, 'out' AS direction,
                   other.name AS other_name, head(labels(other)) AS other_label
            LIMIT 20
            """
        with self._get_driver().session() as s:
            res = s.run(cypher, entity_id=entity_id, depth=depth)
            return [
                {
                    "rel": r["rel"],
                    "direction": r["direction"],
                    "other_name": r["other_name"],
                    "other_label": r["other_label"],
                }
                for r in res
            ]

    # ----- assembler-facing convenience -----
    def retrieve(self, *, query: str, k: int = 4) -> list[RetrievedDoc]:
        """Match entities + 1-hop traverse → one RetrievedDoc per entity.

        Each doc.text is a compact "ENTITY [Label]. Relations: …" string
        the assembler injects into the prompt.
        """
        entities = self.match_entities(query, k=k)
        out: list[RetrievedDoc] = []
        for e in entities:
            edges = self.traverse(e["id"], depth=1)
            edge_strs = [
                f"{ed['rel']}→{ed['other_name']} ({ed['other_label']})"
                if ed["direction"] == "out"
                else f"{ed['other_name']} ({ed['other_label']})→{ed['rel']}"
                for ed in edges
            ]
            text = f"{e['name']} [{e['label']}]"
            if edge_strs:
                text += " — relations: " + "; ".join(edge_strs)
            out.append(
                RetrievedDoc(
                    id=str(e["id"]),
                    text=text,
                    metadata={
                        "entity_name": e["name"],
                        "entity_label": e["label"],
                        "score": e["score"],
                        "neighbours": len(edges),
                    },
                    distance=None,  # KG hits aren't distance-ranked
                )
            )
        return out
