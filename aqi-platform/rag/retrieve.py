"""
retrieve.py

Given a query string, embeds it and returns the top-k most similar
weekly city summaries from aqi_summaries using pgvector cosine similarity.
"""

import os
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
from pathlib import Path
from sentence_transformers import SentenceTransformer

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")
os.environ["HF_TOKEN"] = os.getenv("HF_TOKEN", "")

MODEL_NAME = "all-MiniLM-L6-v2"
_model = None


def _get_model():
    global _model
    if _model is None:
        _model = SentenceTransformer(MODEL_NAME)
    return _model


def retrieve(query: str, conn, k: int = 4) -> list[dict]:
    """
    Returns up to k summaries most semantically similar to the query,
    with at most one summary per city (best match per city, ranked by distance).
    """
    model = _get_model()
    query_embedding = model.encode(query, normalize_embeddings=True).astype('float32')
    vec_str = '[' + ','.join(str(x) for x in query_embedding.tolist()) + ']'

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT DISTINCT ON (city) city, week_start, week_end, summary,
                   embedding <=> %s::vector AS distance
            FROM aqi_summaries
            ORDER BY city, distance ASC
        """, (vec_str,))
        rows = cur.fetchall()

    # sort all best-per-city results by distance, return top k
    rows.sort(key=lambda r: r['distance'])
    return rows[:k]
