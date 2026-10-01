"""
cli.py

Entry point for the AQI RAG system.
Wires together retrieval and generation into an interactive loop.

Usage:
    python rag/cli.py
"""

import os
import psycopg2
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

from retrieve import retrieve
from generate import generate


def main():
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    print("AQI Assistant ready. Type your question or 'quit' to exit.\n")
    try:
        while True:
            query = input("You: ").strip()
            if not query or query.lower() in ("quit", "exit"):
                break

            results = retrieve(query, conn, k=4)
            answer = generate(query, results)
            print(f"\nAssistant: {answer}\n")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
