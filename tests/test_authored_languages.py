import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import authored_languages as A  # noqa


def diff(*files):
    """Build `git log -p -U0` output: files = (header_lines, added_lines)."""
    out = []
    for header, added in files:
        out.extend(header)
        if added:
            out.append("@@ -0,0 +1,%d @@" % len(added))
            out.extend("+" + a for a in added)
    return [(ln + "\n").encode() for ln in out]


def new_file(path, added, plus_suffix=""):
    return ([f"diff --git a/{path} b/{path}", "new file mode 100644",
             "index 0000000..1111111", "--- /dev/null",
             f"+++ b/{path}{plus_suffix}"], added)


def count(lines, attrs=None, sql="SQL", rule=True):
    return A.count_added_bytes(lines, attrs or [], sql, rule)


class PathParsing(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(A.parse_plus_path(b"+++ b/src/Main.java"),
                         "src/Main.java")

    def test_trailing_tab_with_spaces(self):
        self.assertEqual(A.parse_plus_path(b"+++ b/my dir/My File.py\t"),
                         "my dir/My File.py")
        got = count(diff(new_file("my dir/My File.py", ["print(1)"],
                                  plus_suffix="\t")))
        self.assertEqual(got, {"Python": 8})

    def test_dev_null_and_quoted_skipped(self):
        self.assertIsNone(A.parse_plus_path(b"+++ /dev/null"))
        self.assertIsNone(A.parse_plus_path(b'+++ "b/we\\tird.py"'))
        deleted = ([ "diff --git a/A.java b/A.java", "--- a/A.java",
                     "+++ /dev/null"], [])
        self.assertEqual(count(diff(deleted)), {})


class Counting(unittest.TestCase):
    def test_bytes_without_newline_and_utf8(self):
        got = count(diff(new_file("A.java", ["int a;", "// ñ"])))
        self.assertEqual(got, {"Java": 6 + 5})

    def test_rename_pure_counts_nothing(self):
        rename = (["diff --git a/Old.java b/New.java", "similarity index 100%",
                   "rename from Old.java", "rename to New.java"], [])
        self.assertEqual(count(diff(rename)), {})

    def test_rename_with_edit_counts_only_changed_lines_on_new_path(self):
        rename = (["diff --git a/Old.java b/New.java", "similarity index 90%",
                   "rename from Old.java", "rename to New.java",
                   "index 1..2 100644", "--- a/Old.java", "+++ b/New.java"],
                  ["changed line"])
        self.assertEqual(count(diff(rename)), {"Java": 12})

    def test_rename_recounts_identical_lines_known_limitation(self):
        first = new_file("Old.java", ["same line"])
        moved = new_file("New.java", ["same line"])
        self.assertEqual(count(diff(first, moved)), {"Java": 18})

    def test_dedupe_same_line_two_commits(self):
        c1 = new_file("A.java", ["int a;", "int b;"])
        c2 = ([ "diff --git a/A.java b/A.java", "--- a/A.java",
                "+++ b/A.java"], ["int a;", "int c;"])
        self.assertEqual(count(diff(c1, c2)), {"Java": 6 + 6 + 6})

    def test_dedupe_is_per_path(self):
        got = count(diff(new_file("A.java", ["x = 1;"]),
                         new_file("B.java", ["x = 1;"])))
        self.assertEqual(got, {"Java": 12})

    def test_plus_plus_plus_content_line_is_not_a_header(self):
        # an added line "++ foo" shows as "+++ foo" inside a hunk
        lines = diff(new_file("a.py", ["++ foo", "bar"]))
        self.assertEqual(count(lines), {"Python": 6 + 3})

    def test_unknown_and_ignored_extensions(self):
        got = count(diff(new_file("a.yaml", ["k: v"]),
                         new_file("b.zzz", ["x"]),
                         new_file("README", ["x"]),
                         new_file("c.py", ["ok"])))
        self.assertEqual(got, {"Python": 2})


class SqlRule(unittest.TestCase):
    def test_counted_lines(self):
        added = ["CREATE TABLE t (id INT);", "SELECT (1) FROM dual;",
                 "  BEGIN", "-- comment (x)", "UPDATE t SET a = (1) WHERE b;"]
        got = count(diff(new_file("a.sql", added)), sql="PLSQL")
        self.assertEqual(got, {"PLSQL": sum(len(a) for a in added)})

    def test_skipped_data_lines(self):
        added = ["INSERT INTO t VALUES (1, 'a');", "  insert  into t(a)",
                 "('a', 1),", "  (2, 'b');", "(3)", "keep me;"]
        for ext in ("sql", "pls", "pks", "pkb", "plsql"):
            got = count(diff(new_file(f"a.{ext}", added)), sql="PLpgSQL")
            lang = "PLpgSQL" if ext == "sql" else "PLSQL"
            self.assertEqual(got, {lang: len("keep me;")}, ext)

    def test_rule_off_and_non_sql_files_untouched(self):
        added = ["INSERT INTO t VALUES (1);", "(1, 2),"]
        total = sum(len(a) for a in added)
        self.assertEqual(count(diff(new_file("a.sql", added)), rule=False),
                         {"SQL": total})
        self.assertEqual(count(diff(new_file("a.py", added))),
                         {"Python": total})

    def test_is_sql_data(self):
        self.assertTrue(A.is_sql_data(b"INSERT INTO x"))
        self.assertTrue(A.is_sql_data(b"  (1,2)\r"))
        self.assertFalse(A.is_sql_data(b"INSERTED INTO x"))
        self.assertFalse(A.is_sql_data(b"EXECUTE p(1)"))
        self.assertFalse(A.is_sql_data(b"(1) + 2 > x"))


class Exclusions(unittest.TestCase):
    def test_gitattributes_generated_vendored(self):
        attrs = A.parse_gitattributes(
            "# c\n"
            "gen/** linguist-generated\n"
            "third_party/ linguist-vendored=true\n"
            "*.pb.go linguist-generated=true\n"
            "/docs/api.py linguist-vendored\n"
            "keep.py text\n"
            "other.py -linguist-vendored\n")
        for p in ("gen/a/b.py", "third_party/x.py", "sub/m.pb.go",
                  "docs/api.py"):
            self.assertTrue(A.is_excluded(p, attrs), p)
        for p in ("keep.py", "src/docs/api.py", "other.py", "src/gen.py"):
            self.assertFalse(A.is_excluded(p, attrs), p)

    def test_attrs_applied_during_counting(self):
        attrs = A.parse_gitattributes("gen/** linguist-generated\n")
        got = count(diff(new_file("gen/a.java", ["int a;"]),
                         new_file("src/b.java", ["int b;"])), attrs)
        self.assertEqual(got, {"Java": 6})

    def test_builtin_exclusions(self):
        for p in ("package-lock.json", "a/yarn.lock", "x.lock",
                  "static/app.min.js", "a.min.css", "build/A.java",
                  "web/node_modules/x/i.js", "gradlew", ".mvn/wrapper/x.java",
                  "gradle/wrapper/G.java", "a/gradle/wrapper/G.java",
                  "target/classes/A.java", "vendor/x.go"):
            self.assertTrue(A.is_excluded(p, []), p)
        for p in ("src/Main.java", "builder/A.java", "outer/a.py"):
            self.assertFalse(A.is_excluded(p, []), p)


class Mapping(unittest.TestCase):
    def test_extension_map(self):
        cases = {"A.java": "Java", "a.KT": "Kotlin", "a.kts": "Kotlin",
                 "a.tsx": "TypeScript", "a.mjs": "JavaScript",
                 "a.sh": "Shell", "a.pls": "PLSQL", "a.pkb": "PLSQL",
                 "a.h": "C", "a.hpp": "C++", "a.cs": "C#",
                 "a.feature": "Gherkin", "a.j2": "Jinja", "a.tf": "HCL",
                 "a.tex": "TeX", "a.vue": "Vue", "a.ps1": "PowerShell",
                 "Dockerfile": "Dockerfile", "x/Dockerfile.dev": "Dockerfile",
                 "Makefile": "Makefile", "a.sql": "SQL"}
        for path, lang in cases.items():
            self.assertEqual(A.language_for_path(path), lang, path)
        for path in ("a.yaml", "a.json", "a.md", "LICENSE", "a.unknown"):
            self.assertIsNone(A.language_for_path(path), path)

    def test_sql_follows_repo_languages(self):
        self.assertEqual(A.language_for_path("a.sql", "PLSQL"), "PLSQL")
        f = A.sql_language_for_repo
        self.assertEqual(f(["Java", "SQL", "PLSQL"]), "SQL")
        self.assertEqual(f(["Java", "PLSQL", "PLpgSQL"]), "PLSQL")
        self.assertEqual(f(["Java", "PLpgSQL"]), "PLpgSQL")
        self.assertEqual(f(["Java"]), "SQL")
        self.assertEqual(f([]), "SQL")


class Identity(unittest.TestCase):
    def test_matching(self):
        i = A.Identity.from_env(" Me@Example.com, other@x.org ,", "MyLogin")
        self.assertTrue(i.matches("me@example.com"))
        self.assertTrue(i.matches("OTHER@x.org"))
        self.assertTrue(i.matches("123+mylogin@users.noreply.github.com"))
        self.assertTrue(i.matches("mylogin@users.noreply.github.com"))
        self.assertFalse(i.matches("123+someone@users.noreply.github.com"))
        self.assertFalse(i.matches("mylogin@example.com"))
        self.assertFalse(i.matches(""))

    def test_empty(self):
        self.assertEqual(A.Identity.from_env(None, "x").emails, set())


class CacheKey(unittest.TestCase):
    REFS = ["aaa refs/heads/main", "bbb refs/heads/dev"]

    def test_stable_and_order_independent(self):
        self.assertEqual(A.cache_key(self.REFS, "SQL"),
                         A.cache_key(list(reversed(self.REFS)), "SQL"))

    def test_changes_when_refs_change(self):
        base = A.cache_key(self.REFS, "SQL")
        self.assertNotEqual(base, A.cache_key(
            ["ccc refs/heads/main", "bbb refs/heads/dev"], "SQL"))
        self.assertNotEqual(base, A.cache_key(self.REFS[:1], "SQL"))
        self.assertNotEqual(base, A.cache_key(
            self.REFS + ["ddd refs/heads/x"], "SQL"))

    def test_changes_with_algorithm_version_sql_language_and_identity(self):
        base = A.cache_key(self.REFS, "SQL", "id1")
        self.assertNotEqual(base, A.cache_key(self.REFS, "PLSQL", "id1"))
        self.assertNotEqual(base, A.cache_key(self.REFS, "SQL", "id2"))
        old = A.ALGORITHM_VERSION
        try:
            A.ALGORITHM_VERSION = old + 1
            self.assertNotEqual(base, A.cache_key(self.REFS, "SQL", "id1"))
        finally:
            A.ALGORITHM_VERSION = old

    def test_cache_file_name_hides_repo_name(self):
        name = A.cache_file_name("owner/secret-repo")
        self.assertNotIn("secret", name)
        self.assertTrue(name.endswith(".json"))


class FailureCategory(unittest.TestCase):
    def test_only_whitelisted_words_reach_the_log(self):
        f = A.failure_category
        got = f(128, b"fatal: repository 'https://github.com/o/secret-repo.git/'"
                     b" not found")
        self.assertEqual(got, "exit128:fatal-repository-https-not-found")
        self.assertNotIn("secret", got)
        self.assertEqual(f(1, b"weird secret-repo text"), "exit1:")
        self.assertEqual(f(128, b"The requested URL returned error: 403"),
                         "exit128:requested-url-returned-error-403")


if __name__ == "__main__":
    unittest.main()
