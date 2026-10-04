"""
generate.py

Takes a user query and a list of retrieved summaries, calls Groq
(llama-3.3-70b-versatile) and returns a grounded plain-language answer.
"""

import os
from groq import Groq
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(dotenv_path=Path(__file__).resolve().parents[1] / ".env")

_client = None

SYSTEM_PROMPT = """You are an air quality analyst answering questions about Indian cities.
You will be given retrieved sensor data summaries as context.
Rules:
- Use the retrieved data for all specific numbers, city names, and time periods.
- You may use your general knowledge of air pollution science to explain causes and health effects.
- Never invent numbers not present in the retrieved data.
- If an exact SQL lookup is provided, state the applied filters and the exact
  number of underlying sensor rows used when the user asks for it.
- Always cite which city and week each number comes from.
- Be concise and direct.
- Do not use markdown formatting such as **bold**, *italic*, or bullet points with asterisks. Write in plain prose only."""


def _get_client():
    global _client
    if _client is None:
        _client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    return _client


def generate(query: str, retrieved: list[dict], history: list[dict] | None = None,
             sql_context: str = "", sql_rows_used: int = 0,
             sql_filters: dict | None = None) -> str:
    """
    Generates a grounded answer using retrieved summaries and/or exact SQL results.
    history: list of {"role": "user"|"assistant", "content": str} prior turns.
    sql_context: formatted exact rows from the SQL lookup (may be empty).
    """
    parts = []

    if sql_context:
        parts.append(sql_context)
        parts.append(
            f"SQL audit metadata: underlying sensor rows used={sql_rows_used}; "
            f"filters={sql_filters or {}}"
        )

    if retrieved:
        parts.append("Weekly sensor summaries (trend context):")
        parts.append("\n\n".join(
            f"[{r['city']} | {r['week_start']} to {r['week_end']}]\n{r['summary'][:300]}"
            for r in retrieved
        ))

    context = "\n\n".join(parts) if parts else "No relevant sensor data found for this query."

    user_message = f"""Retrieved sensor data:
{context}

Question: {query}"""

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_message})

    response = _get_client().chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=messages,
        temperature=0.2,
    )
    tokens_used = response.usage.total_tokens if response.usage else 0
    return response.choices[0].message.content, tokens_used
