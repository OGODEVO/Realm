import json
import random
import string
from pathlib import Path
from typing import Dict

TAG_FILE = Path("config/agent_tags.json")

def _generate_tag(length: int = 5) -> str:
    """Generate a random 5-character alphanumeric tag (digits + uppercase)."""
    chars = string.ascii_uppercase + string.digits
    return "".join(random.choices(chars, k=length))

def get_or_create_tags(agent_keys: list[str]) -> Dict[str, str]:
    """
    Load existing tags or create new ones for the given agent keys.
    Returns a dictionary mapping agent_key -> tag.
    """
    tags = {}
    if TAG_FILE.exists():
        try:
            tags = json.loads(TAG_FILE.read_text(encoding="utf-8"))
        except Exception:
            tags = {}
    
    updated = False
    for key in agent_keys:
        if key not in tags or not tags[key]:
            tags[key] = _generate_tag()
            updated = True
    
    if updated:
        TAG_FILE.parent.mkdir(parents=True, exist_ok=True)
        TAG_FILE.write_text(json.dumps(tags, indent=2), encoding="utf-8")
        
    return tags
