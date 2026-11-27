"""BOD Perso — Business Opportunity Discovery (version personnelle).

Pipeline : DataForSEO -> Cloud Storage (brut) -> BigQuery (dataset `bod`)
-> Vertex AI (embeddings + Gemini) -> scénarios de rentabilité -> Looker Studio.
"""

__version__ = "1.0.0"
