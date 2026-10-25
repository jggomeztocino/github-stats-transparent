# Languages chart metric

The languages chart (`generated/languages.svg`) measures **bytes of lines
authored by the owner**, not the size of whole repositories.

## Sources (summed)

1. **Own repositories**, computed here by `authored_languages.py` on every run:
   every repository owned by the token's user (private included; forks and
   anything in the `EXCLUDED` secret skipped) is cloned without a working tree
   and every line added by one of the owner's commits is counted.
2. **Work repositories**, published by the owner's work laptop in
   `external/work-languages.json` (see [`external/README.md`](../external/README.md))
   with the same metric, merged by `external_languages.py`.

`EXCLUDED_LANGS` is applied after both, then percentages are computed.

## Rules

- A commit is the owner's when its author email is in the `AUTHOR_EMAILS`
  secret (comma separated) or is a `*@users.noreply.github.com` address that
  contains the owner's login. Merge commits are ignored; all remote branches
  are walked.
- Only added lines count (never subtracts). A line is counted once per
  (repository, path, line content), so squash merges, rebases, cherry-picks and
  deleted branches do not inflate it. A line's size is its byte length without
  the newline.
- Language is derived from the file extension (Linguist names). `.sql` follows
  the repository's own GitHub language (`SQL`, else `PLSQL`, else `PLpgSQL`);
  `.pls .pks .pkb .plsql` are `PLSQL`. YAML, JSON, XML, Markdown, text, CSV and
  SVG are ignored.
- Excluded paths: lock files, `*.min.js`/`*.min.css`, `build`, `target`,
  `dist`, `node_modules`, `vendor`, `generated*`, `.next`, `out`, `coverage`,
  Maven/Gradle wrappers, and anything the repository marks
  `linguist-generated` or `linguist-vendored` in its root `.gitattributes`.
- **SQL data is not code**: in SQL-family files (`.sql .pls .pks .pkb .plsql`),
  added lines matching `^\s*INSERT\s+INTO\b` (case-insensitive) or a value
  tuple `^\s*\(.*\)\s*[,;]?\s*$` are not counted.
- Colors come from the language colors GitHub returns for the repositories;
  unknown languages use `#8b949e`.

## Known limitations

- Renamed files recount their lines (a line added again under a new path is a
  new (path, content) pair).
- Only the root `.gitattributes` is read; paths git quotes (tabs, quotes,
  backslashes in the name) are skipped.
- Data expressed as anything other than the SQL patterns above (for example
  stored-procedure calls used as inserts) still counts as code.

## Operations

- Per-repository results are cached (`actions/cache`, `~/.cache/authored-languages`,
  one file per repository named by a hash). The cache key of a repository is a
  hash of `ALGORITHM_VERSION`, its sorted branch heads (`git ls-remote`), the
  `.sql` attribution and the author identity, so only changed repositories are
  recloned. **Bump `ALGORITHM_VERSION` in `authored_languages.py` whenever a
  rule above changes.**
- The token is passed to git through environment config
  (`http.extraheader`), never in a URL or on a command line. Logs contain
  counts only (`authored languages: N repos, M recloned, K cached`).
- The run fails (nothing is published) if `AUTHOR_EMAILS` is missing, a
  repository can neither be analysed nor served from cache, or the total is
  empty.
- Only per-language aggregate byte counts are published: no repository names,
  emails, paths or line contents.
