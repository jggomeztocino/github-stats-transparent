import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from external_languages import FALLBACK_COLOR, merge_external_languages  # noqa


def iso(days_ago=0):
    t = datetime.now(timezone.utc) - timedelta(days=days_ago)
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def doc(**over):
    d = {"schema": 1, "generated_at": iso(), "window_months": 24,
         "metric": "bytes_added", "languages": {"Java": 100, "Gherkin": 5}}
    d.update(over)
    return d


def base():
    return {"Java": {"size": 1000, "occurrences": 2, "color": "#b07219"}}


class T(unittest.TestCase):
    def run_merge(self, content, langs=None, exclude=None):
        langs = base() if langs is None else langs
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "w.json")
            if content is not None:
                with open(p, "w") as f:
                    f.write(content if isinstance(content, str)
                            else json.dumps(content))
            with contextlib.redirect_stdout(out):
                merge_external_languages(langs, p, exclude)
        return langs, out.getvalue()

    def test_valid(self):
        l, out = self.run_merge(doc())
        self.assertEqual(l["Java"]["size"], 1100)
        self.assertEqual(l["Java"]["color"], "#b07219")
        self.assertEqual(l["Gherkin"]["size"], 5)
        self.assertEqual(l["Gherkin"]["color"], FALLBACK_COLOR)
        self.assertIn("merged 2 work languages", out)
        self.assertNotIn("100", out)

    def test_missing(self):
        l, out = self.run_merge(None)
        self.assertEqual(l, base())
        self.assertEqual(out, "")

    def test_bad(self):
        for bad in (doc(schema=2), doc(metric="x"), doc(languages=[]),
                    doc(languages={"Java": -1}),
                    doc(languages={"Java": 1.5}),
                    doc(languages={"Java": 5, "Go": "7"}),
                    doc(generated_at="nope"), "{not json", "[]"):
            l, out = self.run_merge(bad)
            self.assertEqual(l, base(), bad)
            self.assertIn("::warning::", out)

    def test_stale(self):
        l, out = self.run_merge(doc(generated_at=iso(40)))
        self.assertIn("::warning::work-languages.json is 40 days old", out)
        self.assertEqual(l["Java"]["size"], 1100)

    def test_fresh_no_warning(self):
        _, out = self.run_merge(doc(generated_at=iso(5)))
        self.assertNotIn("::warning::", out)

    def test_excluded(self):
        l, _ = self.run_merge(doc(), exclude={"Gherkin"})
        self.assertNotIn("Gherkin", l)
        self.assertEqual(l["Java"]["size"], 1100)


if __name__ == "__main__":
    unittest.main()
