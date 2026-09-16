import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import certifi
import requests

from scripts.collect_meta_watch_lists import (
    _build_session,
    collect_and_import,
    parse_article_observations,
)


class _FakeResponse:
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


class _FakeSession:
    def __init__(self, mapping):
        self.mapping = mapping

    def get(self, url, timeout=20):
        value = self.mapping[url]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, tuple):
            return _FakeResponse(value[0], status_code=value[1])
        return _FakeResponse(value)


def _article_html(main_count=40, ash_count=3, filler_count_override=None):
    filler_count = filler_count_override if filler_count_override is not None else (main_count - ash_count)
    return f"""
    <html>
      <head>
        <title>YCS Test City Top 32 Deck Lists - Yu-Gi-Oh! TCG Event Coverage</title>
        <meta property="article:published_time" content="2026-09-10T12:00:00Z" />
      </head>
      <body>
        <h3>1st Place: Alice Example - Kashtira</h3>
        <p>Main Deck: {main_count}</p>
        <ul>
          <li>{ash_count} Ash Blossom &amp; Joyous Spring</li>
          <li>{filler_count} Filler Card</li>
        </ul>
        <p>Side Deck: 15</p>
        <ul><li>15 Side Card</li></ul>
        <p>Extra Deck: 15</p>
        <ul><li>15 Extra Card</li></ul>
      </body>
    </html>
    """


class ParsingTests(unittest.TestCase):
    def test_parse_valid_article_preserves_metadata_and_counts(self):
        observations, rejected = parse_article_observations(
            _article_html(),
            "https://yugiohblog.konami.com/2026/ycs/test-city-top-32-deck-lists/",
            {"format": "TCG_ADVANCED", "region": "NA"},
        )
        self.assertEqual(rejected, [])
        self.assertEqual(len(observations), 1)
        obs = observations[0]
        self.assertEqual(obs["player"], "Alice Example")
        self.assertEqual(obs["placement"], "1st Place")
        self.assertEqual(obs["source_url"], "https://yugiohblog.konami.com/2026/ycs/test-city-top-32-deck-lists/")
        self.assertEqual(obs["published_at"], "2026-09-10T12:00:00Z")
        self.assertEqual(sum(c["count"] for c in obs["main_deck"]), 40)
        self.assertEqual(sum(c["count"] for c in obs["side_deck"]), 15)
        self.assertEqual(sum(c["count"] for c in obs["extra_deck"]), 15)

    def test_malformed_article_is_rejected(self):
        observations, rejected = parse_article_observations(
            _article_html(main_count=41, filler_count_override=37),
            "https://yugiohblog.konami.com/2026/ycs/test-city-top-32-deck-lists/",
            {"format": "TCG_ADVANCED", "region": "NA"},
        )
        self.assertEqual(observations, [])
        self.assertTrue(any("count mismatch" in r["reason"] for r in rejected))


class CollectionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dataset_path = str(Path(self.tmp.name) / "meta_watch_lists.json")
        self.report_path = str(Path(self.tmp.name) / "meta_watch_collection_report.json")
        with open(self.dataset_path, "w") as f:
            json.dump({"schema_version": 1, "observations": []}, f)
        self.sources = (
            {"name": "test", "url": "https://yugiohblog.konami.com/category/ycs/", "format": "TCG_ADVANCED", "region": "NA"},
        )
        self.index_html = """
        <html><body>
          <a href="https://yugiohblog.konami.com/2026/ycs/test-city-top-32-deck-lists/">Article</a>
        </body></html>
        """
        self.article_url = "https://yugiohblog.konami.com/2026/ycs/test-city-top-32-deck-lists/"

    def test_network_failures_are_reported_without_erasing_dataset(self):
        session = _FakeSession({
            "https://yugiohblog.konami.com/category/ycs/": requests.ConnectionError("network down"),
        })
        report = collect_and_import(
            dataset_path=self.dataset_path,
            report_path=self.report_path,
            dry_run=False,
            sources=self.sources,
            session=session,
        )
        self.assertEqual(report["import"]["added"], 0)
        self.assertEqual(len(report["source_failures"]), 1)
        with open(self.dataset_path) as f:
            dataset = json.load(f)
        self.assertEqual(dataset["observations"], [])

    def test_repeat_run_dedupes_and_changed_source_lists_are_flagged(self):
        session = _FakeSession({
            "https://yugiohblog.konami.com/category/ycs/": self.index_html,
            self.article_url: _article_html(),
        })
        first = collect_and_import(
            dataset_path=self.dataset_path,
            report_path=self.report_path,
            dry_run=False,
            sources=self.sources,
            session=session,
        )
        self.assertEqual(first["import"]["added"], 1)

        second = collect_and_import(
            dataset_path=self.dataset_path,
            report_path=self.report_path,
            dry_run=False,
            sources=self.sources,
            session=session,
        )
        self.assertEqual(second["import"]["added"], 0)
        self.assertEqual(second["import"]["duplicate_existing_skipped"], 0)
        self.assertEqual(second["duplicate_existing_precheck_skipped"], 1)

        changed_session = _FakeSession({
            "https://yugiohblog.konami.com/category/ycs/": self.index_html,
            self.article_url: _article_html(main_count=40, ash_count=2),
        })
        third = collect_and_import(
            dataset_path=self.dataset_path,
            report_path=self.report_path,
            dry_run=False,
            sources=self.sources,
            session=changed_session,
        )
        self.assertEqual(third["import"]["added"], 0)
        self.assertEqual(third["import"]["duplicate_existing_skipped"], 0)
        self.assertEqual(len(third["changed_source_lists"]), 1)


class WorkflowTests(unittest.TestCase):
    def test_meta_watch_workflow_uses_pr_updates_not_direct_push(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "meta_watch_daily.yml"
        ).read_text()
        self.assertIn("peter-evans/create-pull-request@v7", workflow)
        self.assertNotIn("git push", workflow)
        self.assertIn("scripts.collect_meta_watch_lists", workflow)

    def test_meta_watch_workflow_gates_pr_on_new_observations(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "meta_watch_daily.yml"
        ).read_text()
        self.assertIn("steps.collect.outputs.observations_added", workflow)
        # Ensure the gate is on the PR-opening step, not just informational
        self.assertRegex(
            workflow,
            r"open automated meta watch data-update PR[\s\S]*?if:[^\n]*steps\.collect\.outputs\.observations_added",
        )

    def test_meta_watch_workflow_never_disables_tls(self):
        workflow = (
            Path(__file__).resolve().parents[1]
            / ".github"
            / "workflows"
            / "meta_watch_daily.yml"
        ).read_text()
        self.assertNotIn("curl -k", workflow)
        self.assertNotIn("--insecure", workflow)
        self.assertNotIn("verify=False", workflow)


class TlsAndFailureTests(unittest.TestCase):
    def test_default_session_uses_certifi_bundle(self):
        session = _build_session()
        try:
            self.assertEqual(session.verify, certifi.where())
            self.assertTrue(os.path.exists(session.verify))
        finally:
            session.close()

    def test_collector_never_disables_tls_verification(self):
        source = Path(__file__).resolve().parents[1] / "scripts" / "collect_meta_watch_lists.py"
        text = source.read_text()
        self.assertNotIn("verify=False", text)
        self.assertNotIn("verify = False", text)
        self.assertNotIn("VERIFY_NONE", text)

    def test_all_sources_failed_flag_and_cli_exit_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset_path = str(Path(tmp) / "meta_watch_lists.json")
            report_path = str(Path(tmp) / "report.json")
            with open(dataset_path, "w") as f:
                json.dump({"schema_version": 1, "observations": []}, f)

            sources = (
                {"name": "a", "url": "https://yugiohblog.konami.com/category/ycs/", "format": "TCG_ADVANCED", "region": "NA"},
                {"name": "b", "url": "https://yugiohblog.konami.com/category/championships/", "format": "TCG_ADVANCED", "region": "NA"},
            )
            session = _FakeSession({
                sources[0]["url"]: requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED"),
                sources[1]["url"]: requests.exceptions.SSLError("CERTIFICATE_VERIFY_FAILED"),
            })
            report = collect_and_import(
                dataset_path=dataset_path,
                report_path=report_path,
                dry_run=False,
                sources=sources,
                session=session,
            )
            self.assertTrue(report["all_sources_failed"])
            self.assertEqual(len(report["source_failures"]), 2)
            self.assertEqual(report["import"]["added"], 0)
            with open(dataset_path) as f:
                self.assertEqual(json.load(f)["observations"], [])

    def test_partial_source_failure_does_not_mark_all_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset_path = str(Path(tmp) / "meta_watch_lists.json")
            report_path = str(Path(tmp) / "report.json")
            with open(dataset_path, "w") as f:
                json.dump({"schema_version": 1, "observations": []}, f)
            sources = (
                {"name": "a", "url": "https://yugiohblog.konami.com/category/ycs/", "format": "TCG_ADVANCED", "region": "NA"},
                {"name": "b", "url": "https://yugiohblog.konami.com/category/championships/", "format": "TCG_ADVANCED", "region": "NA"},
            )
            session = _FakeSession({
                sources[0]["url"]: "<html></html>",
                sources[1]["url"]: requests.exceptions.SSLError("boom"),
            })
            report = collect_and_import(
                dataset_path=dataset_path,
                report_path=report_path,
                dry_run=False,
                sources=sources,
                session=session,
            )
            self.assertFalse(report["all_sources_failed"])
            self.assertEqual(len(report["source_failures"]), 1)

    def test_cli_exits_nonzero_and_emits_outputs_when_all_sources_fail(self):
        repo_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            dataset_path = Path(tmp) / "meta_watch_lists.json"
            report_path = Path(tmp) / "report.json"
            gh_output = Path(tmp) / "gh_output"
            gh_output.touch()
            with open(dataset_path, "w") as f:
                json.dump({"schema_version": 1, "observations": []}, f)

            # Point the two default sources at a non-routable localhost port so
            # every request fails without hitting the network.
            wrapper = Path(tmp) / "run.py"
            wrapper.write_text(
                "import sys\n"
                f"sys.path.insert(0, {str(repo_root)!r})\n"
                "from scripts import collect_meta_watch_lists as m\n"
                "m.DEFAULT_SOURCE_INDEXES = (\n"
                "    {'name': 'a', 'url': 'http://127.0.0.1:1/one/', 'format': 'TCG_ADVANCED', 'region': 'NA'},\n"
                "    {'name': 'b', 'url': 'http://127.0.0.1:1/two/', 'format': 'TCG_ADVANCED', 'region': 'NA'},\n"
                ")\n"
                "m.main()\n"
            )
            env = dict(os.environ)
            env["GITHUB_OUTPUT"] = str(gh_output)
            result = subprocess.run(
                [
                    sys.executable,
                    str(wrapper),
                    "--dataset",
                    str(dataset_path),
                    "--report",
                    str(report_path),
                    "--timeout",
                    "1",
                ],
                cwd=str(repo_root),
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2, msg=result.stderr)
            output_contents = gh_output.read_text()
            self.assertIn("observations_added=0", output_contents)
            self.assertIn("all_sources_failed=true", output_contents)


if __name__ == "__main__":
    unittest.main()
