#!/usr/bin/env python3
"""Merge aggregated per-language byte counts from external/work-languages.json."""

import json
import os
from datetime import datetime, timezone
from typing import Dict, Optional, Set

DEFAULT_PATH = os.path.join("external", "work-languages.json")
FALLBACK_COLOR = "#8b949e"
STALE_DAYS = 30


def _load_valid(path: str) -> Optional[Dict[str, int]]:
    """Return the validated languages dict, or None (after a warning)."""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("top level is not an object")
        if data.get("schema") != 1 or isinstance(data.get("schema"), bool):
            raise ValueError("schema is not 1")
        if data.get("metric") != "bytes_added":
            raise ValueError("metric is not bytes_added")
        langs = data.get("languages")
        if not isinstance(langs, dict):
            raise ValueError("languages is not an object")
        for k, v in langs.items():
            if (not isinstance(k, str) or not k or isinstance(v, bool)
                    or not isinstance(v, int) or v < 0):
                raise ValueError("languages has an invalid entry")
        generated = data.get("generated_at")
        if not isinstance(generated, str):
            raise ValueError("generated_at missing")
        ts = datetime.fromisoformat(generated.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
    except (OSError, ValueError, TypeError) as e:
        print(f"::warning::ignoring {path}: {e}")
        return None
    age = (datetime.now(timezone.utc) - ts).days
    if age > STALE_DAYS:
        print(f"::warning::work-languages.json is {age} days old")
    return langs


def merge_external_languages(languages: Dict, path: str = DEFAULT_PATH,
                             exclude_langs: Optional[Set] = None) -> Dict:
    """Add external byte counts into languages ({name: {size, occurrences,
    color}}) in place and return it. Invalid or missing file: no change."""
    langs = _load_valid(path)
    if langs is None:
        return languages
    exclude = exclude_langs or set()
    merged = 0
    for name, size in langs.items():
        if name in exclude:
            continue
        if name in languages:
            languages[name]["size"] += size
        else:
            languages[name] = {"size": size, "occurrences": 1,
                               "color": FALLBACK_COLOR}
        merged += 1
    print(f"merged {merged} work languages")
    return languages
