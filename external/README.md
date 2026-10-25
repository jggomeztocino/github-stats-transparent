# external/

Optional input for the stats generator: `work-languages.json`.

- What: aggregated per-language byte counts for code the owner wrote in
  employer repositories, which GitHub's own language stats cannot see.
- Who writes it: a scheduled script on the owner's work laptop, which pushes
  it to `main`. Only per-language totals; no repo names or any other data.
- Merge: `external_languages.py`, called from `github_stats.py` after the
  owner's own repositories' authored languages (`authored_languages.py`, same
  metric; see [`docs/authored-languages.md`](../docs/authored-languages.md))
  are computed and before percentages. `EXCLUDED_LANGS`
  still applies to merged languages.
- Invalid or missing file: warning logged, file ignored, nothing merged.
  Older than 30 days: Actions warning, still merged.

```json
{"schema": 1, "generated_at": "2026-10-25T20:00:00Z", "window_months": null,
 "metric": "bytes_added", "languages": {"Java": 1234567, "Gherkin": 23456}}
```

`languages` keys are GitHub Linguist names; values are non-negative integer
bytes of lines added in the window.
`window_months` may be `null` = the author's full history (the current publisher uses
`null`). The publisher counts bytes of added lines deduplicated per (repository, path,
line content), additive only (never subtracts), so squash merges, rebases, cherry-picks
and deleted branches are counted once. Known limitation: renamed files recount their lines. Languages unknown from repos use `#8b949e`.

The metric is the same one the generator computes for the owner's own
repositories (bytes of authored lines, per-line dedupe, additive), so both
sources add up consistently. The work publisher should also skip the
SQL-data lines described in `docs/authored-languages.md` (`INSERT INTO` and
value-tuple lines in SQL-family files) to stay comparable. Only aggregates are
published.
