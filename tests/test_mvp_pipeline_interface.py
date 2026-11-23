import json
import tempfile
import unittest
from pathlib import Path

from autoscout.pipeline import run_pipeline


class MvpPipelineInterfaceTests(unittest.TestCase):
    def test_named_mvp_pipeline_interface_runs_existing_artifact_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            output_dir = tmp_path / "runs"

            run_pipeline(
                Path("configs/weekly-mvp-fixture.json"),
                output_dir,
            )

            self.assertTrue((output_dir / "editor-facing.csv").exists())
            self.assertTrue((output_dir / "run-summary.json").exists())
            self.assertTrue((output_dir / "source-mentions.json").exists())
            summary = json.loads((output_dir / "run-summary.json").read_text(encoding="utf-8"))
            self.assertIn("editor_facing_count", summary)
            self.assertIn("major_warnings", summary)
