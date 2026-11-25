import io
import json
import unittest
from emulator.generate_lock import generate


class LockTests(unittest.TestCase):
    def test_hashes(self):
        def fetch(url, timeout):
            self.assertIn("/demo/1.0/json", url)
            return io.StringIO(json.dumps({
                "info": {"name": "demo", "version": "1.0"},
                "urls": [
                    {"packagetype": "bdist_wheel", "digests": {"sha256": "a" * 64}},
                    {"packagetype": "sdist", "digests": {"sha256": "b" * 64}, "yanked": True},
                    {"packagetype": "sdist", "digests": {"sha256": "c" * 64}},
                ],
            }))
        result = generate("demo==1.0\n", fetch)
        self.assertIn("demo==1.0 --hash=sha256:" + "a" * 64, result)
        self.assertNotIn("b" * 64, result)
        self.assertIn("--hash=sha256:" + "c" * 64, result)

    def test_reject_unpinned(self):
        with self.assertRaises(ValueError):
            generate("demo>=1.0")

    def test_committed_lock_matches_pins(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        def pins(path):
            return [line.split()[0] for line in path.read_text().splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
        self.assertEqual(pins(root/"requirements.pins"), pins(root/"requirements.lock"))
