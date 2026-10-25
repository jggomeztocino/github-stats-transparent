# external/

Optional input for the stats generator: `work-languages.json`.

- What: aggregated per-language byte counts for code the owner wrote in
  employer repositories, which GitHub's own language stats cannot see.
- Who writes it: a scheduled script on the owner's work laptop, which pushes
  it to `main`. Only per-language totals; no repo names or any other data.
- Merge: `external_languages.py`, called from `github_stats.py` after the
  GitHub repo languages are summed and before percentages. `EXCLUDED_LANGS`
  still applies to merged languages.
- Invalid or missing file: warning logged, file ignored, nothing merged.
  Older than 30 days: Actions warning, still merged.

```json
{"schema": 1, "generated_at": "2026-10-25T20:00:00Z", "window_months": 24,
 "metric": "bytes_added", "languages": {"Java": 1234567, "Gherkin": 23456}}
```

`languages` keys are GitHub Linguist names; values are non-negative integer
bytes of lines added in the window. Languages unknown from repos use `#8b949e`.
