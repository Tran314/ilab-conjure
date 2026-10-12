from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class ReliabilityFrontendTests(unittest.TestCase):
    def test_reliability_recovery(self):
        node = shutil.which("node")
        esbuild = Path("node_modules/.bin/esbuild")
        if node is None or not esbuild.exists():
            self.skipTest("node and npm install are required")
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "reliability-recovery.test.mjs"
            for command in ([str(esbuild), "tests/frontend/reliability_recovery.test.ts", "--bundle", "--platform=node", "--format=esm", "--target=node20", f"--outfile={output}", "--log-level=warning"], [node, "--test", str(output)]):
                result = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
