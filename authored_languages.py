#!/usr/bin/env python3
"""Languages chart metric: bytes of lines authored by the user.

For every repository owned by the token's user (private included, forks and
EXCLUDED skipped) the history is cloned (no checkout) and every line added by
one of the user's commits is counted, deduplicated per (path, line content)
within the repository and additive only (never subtracts). The result is a
dict language -> bytes, the same metric the work laptop publishes in
external/work-languages.json.

Everything except `fetch_owned_repos` / `run` is pure (no network) so it can be
unit tested. Only aggregates are ever logged or cached: no repository names,
emails or paths reach the logs; the on-disk cache names files by a hash.
"""

import base64
import collections
import hashlib
import json
import os
import re
import subprocess
import tempfile
from typing import Dict, Iterable, List, Optional, Set, Tuple

import requests

# Bump whenever any counting rule below changes: invalidates every cache entry.
ALGORITHM_VERSION = 1
FALLBACK_COLOR = "#8b949e"
NOREPLY_DOMAIN = "@users.noreply.github.com"
CLONE_TIMEOUT = 3600
LOG_TIMEOUT = 3600

# Linguist language per file extension (lowercase, no dot).
LANGUAGE_EXTENSIONS = {
    "Java": "java", "Kotlin": "kt kts", "Python": "py", "TypeScript": "ts tsx",
    "JavaScript": "js mjs cjs jsx", "Shell": "sh bash", "SQL": "sql",
    "PLSQL": "pls pks pkb plsql", "C": "c h", "C++": "cpp cc cxx hpp hh",
    "C#": "cs", "HTML": "html htm", "CSS": "css", "SCSS": "scss",
    "Jinja": "j2 jinja jinja2", "Gherkin": "feature", "PowerShell": "ps1",
    "Go": "go", "Groovy": "groovy", "Dockerfile": "dockerfile", "TeX": "tex",
    "HCL": "hcl tf", "Vue": "vue",
}
EXT_TO_LANG: Dict[str, str] = {}
for _lang, _exts in LANGUAGE_EXTENSIONS.items():
    for _e in _exts.split():
        EXT_TO_LANG[_e] = _lang

# Extensions that are data/prose/config, not code: ignored.
IGNORED_EXTENSIONS = {"yaml", "yml", "json", "xml", "md", "txt", "csv", "svg"}
EXCLUDED_DIRS = {"build", "target", "dist", "node_modules", "vendor",
                 "generated", "generated-sources", "generated-test-sources",
                 ".next", "out", "coverage"}
LOCK_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml",
              "poetry.lock", "gradle.lockfile", "go.sum", "gradlew",
              "gradlew.bat", "mvnw"}
SQL_EXTENSIONS = {"sql", "pls", "pks", "pkb", "plsql"}
# SQL data is not code: INSERT statements and value-tuple continuation lines.
_SQL_INSERT = re.compile(rb"^\s*INSERT\s+INTO\b", re.IGNORECASE)
_SQL_TUPLE = re.compile(rb"^\s*\(.*\)\s*[,;]?\s*$")


# --------------------------------------------------------------------------
# Pure rules
# --------------------------------------------------------------------------

def sql_language_for_repo(linguist_names: Iterable[str]) -> str:
    """`.sql` is attributed to what the repo's own Linguist languages say."""
    names = set(linguist_names)
    if "SQL" in names:
        return "SQL"
    if "PLSQL" in names:
        return "PLSQL"
    if "PLpgSQL" in names:
        return "PLpgSQL"
    return "SQL"


def is_sql_data(line: bytes) -> bool:
    """True for added SQL lines that are pure data (INSERT INTO / tuples)."""
    return bool(_SQL_INSERT.match(line) or _SQL_TUPLE.match(line))


def glob_to_regex(glob: str):
    """Translate a .gitattributes path pattern into a compiled regex."""
    glob = glob.strip()
    is_dir = glob.endswith("/")
    glob = glob.rstrip("/")
    anchored = glob.startswith("/") or "/" in glob
    glob = glob.lstrip("/")
    out = []
    i = 0
    while i < len(glob):
        c = glob[i]
        if glob[i:i + 3] == "**/":
            out.append("(?:.*/)?")
            i += 3
            continue
        if glob[i:i + 2] == "**":
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    prefix = "^" if anchored else "(?:^|/)"
    suffix = "/.*$" if is_dir else "(?:/.*)?$"
    return re.compile(prefix + "".join(out) + suffix)


_ATTRS = {"linguist-generated", "linguist-vendored",
          "linguist-generated=true", "linguist-vendored=true"}


def parse_gitattributes(text: str) -> List:
    """Regexes of the paths marked linguist-generated / linguist-vendored."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 2 or not any(a in _ATTRS for a in fields[1:]):
            continue
        try:
            out.append(glob_to_regex(fields[0]))
        except re.error:
            pass
    return out


def is_excluded(path: str, attr_patterns: List) -> bool:
    """Lock files, minified, build/vendor dirs, wrappers, linguist-marked."""
    parts = path.split("/")
    base = parts[-1]
    if (base in LOCK_FILES or base.endswith(".lock")
            or base.endswith(".min.js") or base.endswith(".min.css")):
        return True
    if any(d in EXCLUDED_DIRS for d in parts[:-1]):
        return True
    if (".mvn" in parts[:-1] or path.startswith("gradle/wrapper/")
            or "/gradle/wrapper/" in path):
        return True
    return any(r.search(path) for r in attr_patterns)


def language_for_path(path: str, sql_language: str = "SQL") -> Optional[str]:
    """Linguist language of a path, or None when it is not counted code."""
    base = path.split("/")[-1]
    if base == "Dockerfile" or base.startswith("Dockerfile."):
        return "Dockerfile"
    if base == "Makefile":
        return "Makefile"
    if "." not in base:
        return None
    ext = base.rsplit(".", 1)[1].lower()
    if ext == "sql":
        return sql_language
    return EXT_TO_LANG.get(ext)


def parse_plus_path(line: bytes) -> Optional[str]:
    """Path of a `+++ ` header line, or None (deleted/quoted: not counted).

    Git appends a trailing TAB to the path when it contains spaces."""
    path = line[4:].decode("utf-8", errors="replace").rstrip("\t")
    if path.startswith("b/"):
        path = path[2:]
    if path == "/dev/null" or path.startswith('"'):
        return None
    return path


def count_added_bytes(diff_lines: Iterable[bytes], attr_patterns: List,
                      sql_language: str = "SQL",
                      sql_data_rule: bool = True) -> Dict[str, int]:
    """Bytes of added lines per language from `git log -p -U0 -M` output.

    Lines are deduplicated per (path, content): the same line added again in
    another commit (or on another branch) counts once. Line bytes exclude the
    newline. Renames (-M) are attributed to the new path."""
    seen: Set[bytes] = set()
    totals: collections.Counter = collections.Counter()
    path = None
    in_hunk = False
    skip = True
    language = None
    sql_file = False
    for raw in diff_lines:
        line = raw.rstrip(b"\n")
        if line.startswith(b"diff --git "):
            in_hunk, path, skip = False, None, True
            continue
        if not in_hunk:
            if line.startswith(b"+++ "):
                path = parse_plus_path(line)
                if path is None:
                    skip = True
                else:
                    skip = is_excluded(path, attr_patterns)
                    language = language_for_path(path, sql_language)
                    base = path.split("/")[-1]
                    sql_file = ("." in base and
                                base.rsplit(".", 1)[1].lower() in SQL_EXTENSIONS)
            elif line.startswith(b"@@"):
                in_hunk = True
            continue
        if line.startswith(b"@@"):
            continue
        if not line.startswith(b"+") or skip or path is None:
            continue
        content = line[1:]
        if language is None:
            continue
        if sql_data_rule and sql_file and is_sql_data(content):
            continue
        key = hashlib.blake2b(path.encode() + b"\0" + content,
                              digest_size=16).digest()
        if key in seen:
            continue
        seen.add(key)
        totals[language] += len(content)
    return dict(totals)


class Identity:
    """Which commit author emails are the user's."""

    def __init__(self, emails: Iterable[str], login: str):
        self.emails = {e.strip().lower() for e in emails if e.strip()}
        self.login = login.strip().lower()

    @classmethod
    def from_env(cls, value: Optional[str], login: str) -> "Identity":
        return cls((value or "").split(","), login)

    def matches(self, email: str) -> bool:
        email = email.strip().lower()
        if email in self.emails:
            return True
        return bool(self.login and email.endswith(NOREPLY_DOMAIN)
                    and self.login in email[:-len(NOREPLY_DOMAIN)])

    def digest(self) -> str:
        data = "\n".join(sorted(self.emails)) + "\0" + self.login
        return hashlib.sha256(data.encode()).hexdigest()


def cache_key(refs: Iterable[str], sql_language: str,
              identity_digest: str = "") -> str:
    """Per-repo cache key: changes with the algorithm, the refs (tips), the
    .sql attribution or the author identity."""
    h = hashlib.sha256()
    h.update(f"v{ALGORITHM_VERSION}\0{sql_language}\0{identity_digest}\0".encode())
    h.update("\n".join(sorted(refs)).encode())
    return h.hexdigest()


def cache_file_name(repo: str) -> str:
    """Repo names never appear on disk: files are named by a hash."""
    return hashlib.sha256(repo.encode()).hexdigest()[:32] + ".json"


# --------------------------------------------------------------------------
# Git / network (not unit tested)
# --------------------------------------------------------------------------

def _git_env(token: str) -> Dict[str, str]:
    """Environment that authenticates git over HTTPS without putting the token
    in a URL or argv."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    env = dict(os.environ)
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"Authorization: basic {basic}",
    })
    return env


def failure_category(returncode: int, stderr: bytes) -> str:
    """Coarse, name-free reason for a failed git call (safe for public logs)."""
    text = stderr.decode(errors="replace").lower()
    if "authentication failed" in text or "could not read username" in text:
        return "auth"
    if "not found" in text:
        return "notfound"
    if "403" in text or "denied" in text or "forbidden" in text:
        return "denied"
    if "timed out" in text or "could not resolve" in text:
        return "network"
    return f"exit{returncode}"


FAILURES: collections.Counter = collections.Counter()


def _url(repo: str) -> str:
    return f"https://github.com/{repo}.git"


def list_heads(repo: str, env: Dict[str, str]) -> Optional[List[str]]:
    """`<sha> <ref>` lines of the repo's branch heads, or None on failure."""
    try:
        r = subprocess.run(["git", "ls-remote", "--heads", _url(repo)],
                           env=env, capture_output=True, timeout=300)
    except subprocess.TimeoutExpired:
        FAILURES["ls-remote timeout"] += 1
        return None
    if r.returncode != 0:
        FAILURES["ls-remote " + failure_category(r.returncode, r.stderr)] += 1
        return None
    return [ln.replace("\t", " ") for ln in
            r.stdout.decode(errors="replace").splitlines() if ln.strip()]


def analyze_repo(repo: str, env: Dict[str, str], identity: Identity,
                 sql_language: str) -> Optional[Dict[str, int]]:
    """Clone (no checkout) into a temp dir, count, remove. None on failure."""
    with tempfile.TemporaryDirectory(prefix="authored-") as tmp:
        d = os.path.join(tmp, "r")
        try:
            r = subprocess.run(["git", "clone", "--quiet", "--no-checkout",
                                _url(repo), d], env=env,
                               capture_output=True, timeout=CLONE_TIMEOUT)
            if r.returncode != 0:
                FAILURES["clone " + failure_category(r.returncode, r.stderr)] += 1
                return None
            ga = subprocess.run(["git", "-C", d, "show", "HEAD:.gitattributes"],
                                env=env, capture_output=True, timeout=300)
            attrs = (parse_gitattributes(ga.stdout.decode(errors="replace"))
                     if ga.returncode == 0 else [])
            lg = subprocess.run(
                ["git", "-C", d, "log", "--remotes", "--no-merges",
                 "--format=%H %ae"], env=env, capture_output=True,
                timeout=LOG_TIMEOUT)
            if lg.returncode != 0:
                return None
            shas = []
            for ln in lg.stdout.decode(errors="replace").splitlines():
                sha, _, email = ln.partition(" ")
                if sha and identity.matches(email):
                    shas.append(sha)
            if not shas:
                return {}
            shas_file = os.path.join(tmp, "shas")
            with open(shas_file, "w") as f:
                f.write("\n".join(shas) + "\n")
            with open(shas_file, "rb") as stdin:
                p = subprocess.Popen(
                    ["git", "-c", "core.quotepath=false",
                     "-c", "diff.mnemonicPrefix=false",
                     "-c", "diff.noprefix=false", "-C", d, "log",
                     "--no-walk=unsorted", "--stdin", "--format=", "-p",
                     "--unified=0", "--no-color", "--no-ext-diff",
                     "--no-textconv", "-M"],
                    env=env, stdin=stdin, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL)
                try:
                    totals = count_added_bytes(p.stdout, attrs, sql_language)
                    p.wait(timeout=LOG_TIMEOUT)
                finally:
                    if p.poll() is None:
                        p.kill()
                    p.stdout.close()
            if p.returncode != 0:
                return None
            return totals
        except (subprocess.TimeoutExpired, OSError):
            return None


def fetch_owned_repos(token: str) -> Tuple[str, List[Dict]]:
    """(login, repos) via GraphQL: owner-affiliated, non-fork, private
    included. Each repo: nameWithOwner + {language name: color}."""
    query = """query($after: String) { viewer { login
      repositories(first: 100, after: $after, isFork: false,
                   ownerAffiliations: OWNER) {
        pageInfo { hasNextPage endCursor }
        nodes { nameWithOwner
                languages(first: 20) { edges { node { name color } } } } } } }"""
    login = ""
    repos: List[Dict] = []
    after = None
    while True:
        data = None
        for _ in range(3):
            try:
                r = requests.post(
                    "https://api.github.com/graphql",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"query": query, "variables": {"after": after}},
                    timeout=60)
                data = r.json().get("data")
                if data:
                    break
            except (requests.RequestException, ValueError):
                pass
        if not data or not data.get("viewer"):
            raise RuntimeError("could not list repositories via GraphQL")
        viewer = data["viewer"]
        login = viewer["login"]
        page = viewer["repositories"]
        for node in page["nodes"]:
            langs = {e["node"]["name"]: e["node"].get("color")
                     for e in node["languages"]["edges"]}
            repos.append({"name": node["nameWithOwner"], "languages": langs})
        if not page["pageInfo"]["hasNextPage"]:
            return login, repos
        after = page["pageInfo"]["endCursor"]


def _read_cache(path: str) -> Optional[Dict]:
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if (isinstance(data, dict) and isinstance(data.get("key"), str)
                and isinstance(data.get("bytes"), dict)):
            return data
    except (OSError, ValueError):
        pass
    return None


def _write_cache(path: str, key: str, totals: Dict[str, int]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"key": key, "bytes": totals}, f, sort_keys=True)
    os.replace(tmp, path)


def run(token: str, emails_value: Optional[str],
        exclude_repos: Optional[Set[str]] = None,
        cache_dir: Optional[str] = None) -> Dict[str, Dict]:
    """Authored bytes per language: {name: {size, occurrences, color}}.

    Raises RuntimeError (generic message) when the identity is missing, a repo
    could not be analysed and has no cached result, or the total is empty."""
    cache_dir = os.path.expanduser(cache_dir or "~/.cache/authored-languages")
    os.makedirs(cache_dir, exist_ok=True)
    login, repos = fetch_owned_repos(token)
    identity = Identity.from_env(emails_value, login)
    if not identity.emails:
        raise RuntimeError("AUTHOR_EMAILS is empty or missing")
    exclude = exclude_repos or set()
    repos = [r for r in repos if r["name"] not in exclude]
    env = _git_env(token)
    id_digest = identity.digest()

    colors: Dict[str, str] = {}
    sizes: collections.Counter = collections.Counter()
    occurrences: collections.Counter = collections.Counter()
    recloned = cached = stale = failed = 0
    live_files = set()
    for repo in repos:
        for name, color in repo["languages"].items():
            if color:
                colors.setdefault(name, color)
        sql_lang = sql_language_for_repo(repo["languages"])
        fname = cache_file_name(repo["name"])
        live_files.add(fname)
        path = os.path.join(cache_dir, fname)
        entry = _read_cache(path)
        refs = list_heads(repo["name"], env)
        result = None
        if refs == []:
            result = {}  # empty repository: nothing to clone
            cached += 1
        elif refs is not None:
            key = cache_key(refs, sql_lang, id_digest)
            if entry and entry["key"] == key:
                result = entry["bytes"]
                cached += 1
            else:
                result = analyze_repo(repo["name"], env, identity, sql_lang)
                if result is not None:
                    _write_cache(path, key, result)
                    recloned += 1
        if result is None:
            if entry:
                result = entry["bytes"]
                stale += 1
            else:
                failed += 1
                continue
        for lang, size in result.items():
            sizes[lang] += size
            occurrences[lang] += 1
    for fname in os.listdir(cache_dir):
        if fname.endswith(".json") and fname not in live_files:
            os.remove(os.path.join(cache_dir, fname))
    msg = (f"authored languages: {len(repos)} repos, {recloned} recloned, "
           f"{cached} cached")
    if stale:
        msg += f", {stale} stale"
    print(msg)
    if failed:
        reasons = ", ".join(f"{k} x{v}" for k, v in sorted(FAILURES.items()))
        raise RuntimeError(f"{failed} repositories could not be analysed "
                           f"and have no cached result ({reasons})")
    sizes = {k: v for k, v in sizes.items() if v > 0}
    if not sizes:
        raise RuntimeError("authored languages total is empty "
                           "(AUTHOR_EMAILS wrong?)")
    return {lang: {"size": size, "occurrences": occurrences[lang],
                   "color": colors.get(lang, FALLBACK_COLOR)}
            for lang, size in sizes.items()}
