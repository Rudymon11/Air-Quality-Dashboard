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
- Always cite which city and week each number comes from.
- Be concise and direct."""


def _get_client():
    global _client
    if _client is None:
        _client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    return _client


def generate(query: str, retrieved: list[dict]) -> str:
    """
    Generates a grounded answer using retrieved summaries as context.
    """
    if not retrieved:
        context = "No relevant sensor data found for this query."
    else:
        context = "\n\n".join(
            f"[{r['city']} | {r['week_start']} to {r['week_end']}]\n{r['summary']}"
            for r in retrieved
        )

    user_message = f"""Retrieved sensor data:
{context}

Question: {query}"""

    response = _get_client().chat.completions.create(
        model="openai/gpt-oss-120b",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_message},
        ],
        temperature=0.2,
    )
    return response.choices[0].message.content
