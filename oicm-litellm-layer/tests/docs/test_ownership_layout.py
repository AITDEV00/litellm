from pathlib import Path
import tempfile
import unittest

DOCS = Path(__file__).resolve().parents[2] / "docs"
RETIRED_FAMILIES = (
    "SSL", "access-control", "admin-api", "architecture", "cache-invalidation",
    "client-distribution", "components", "custom-providers", "custom-routes-plans",
    "dashboard-plan", "discovery-controller", "htb-rate-limiting", "incidents",
    "mcp-source-broker", "model-pricing", "oicm-status", "openrouter", "performance",
    "persona-drift", "reports", "runbooks", "session-identity", "techniques", "usage-guides",
)


def retired_entries(docs_root: Path) -> tuple[Path, ...]:
    return tuple(
        entry
        for name in RETIRED_FAMILIES
        for entry in path_entries(docs_root / name)
    )


def path_entries(path: Path) -> tuple[Path, ...]:
    if path.is_symlink() or path.is_file():
        return (path,)
    return (path, *sorted(path.rglob("*"))) if path.is_dir() else ()


class DocumentationArtifactTest(unittest.TestCase):
    def test_no_retired_directory_or_artifact_remains(self):
        self.assertEqual(retired_entries(DOCS), ())

    def test_rejects_non_markdown_leftovers_and_empty_folders(self):
        with tempfile.TemporaryDirectory() as directory:
            docs = Path(directory)
            incident = docs / "incidents/2026-09-08-postgres-disk-full"
            incident.mkdir(parents=True)
            self.assertEqual(retired_entries(docs), (docs / "incidents", incident))
            (incident / "index.html").write_text("<html></html>")
            (docs / "oicm-status/evidence").mkdir(parents=True)
            self.assertEqual(len(retired_entries(docs)), 5)

    def test_recovered_artifacts_support_their_consumers(self):
        evidence = DOCS / "platform/oicm-status/evidence"
        self.assertEqual(len(list(evidence.glob("*.json"))), 13)
        self.assertEqual(len(list(evidence.glob("*.sse"))), 1)
        html = DOCS / "operations/incidents/2026-09-08-postgres-disk-full/index.html"
        self.assertEqual(len(html.read_text().splitlines()), 559)
        self.assertIn("<title>Sep 08 2026: what happened</title>", html.read_text())


if __name__ == "__main__":
    unittest.main()
