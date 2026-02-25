import os
import requests
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

PERPLEXITY_API_URL = "https://api.perplexity.ai/chat/completions"

def search_web(query: str) -> str:
    """
    Perform a web search using Perplexity API.
    """
    api_key = os.getenv("PERPLEXITY_API_KEY")
    if not api_key:
        return "Error: PERPLEXITY_API_KEY not found."

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    # "sonar" models are good for search
    payload = {
        "model": "sonar-pro", 
        "messages": [
            {"role": "system", "content": "You are a helpful search assistant. Return concise, factual summaries."},
            {"role": "user", "content": query}
        ],
        "temperature": 0.2
    }

    try:
        response = requests.post(PERPLEXITY_API_URL, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        logger.error(f"Perplexity search failed: {e}")
        return f"Error searching web: {str(e)}"

def get_nba_news(query: str) -> str:
    """
    Search for real-time NBA news, injury reports, rade rumors, and team chemistry/vibes.
    
    CRITICAL: 
    - Do NOT use this tool for box scores, player stats, or historical game data. Use nba_tools for that.
    - Use this ONLY for qualitative info ("vibes", "locker room issues") or very recent breaking news (e.g. "is Luka playing tonight?").
    """
    # Force context to be NBA related if not obvious
    enhanced_query = f"NBA news: {query}" if "nba" not in query.lower() else query
    return search_web(enhanced_query)
