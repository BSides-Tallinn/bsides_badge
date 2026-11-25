import hashlib
import io
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from emulator.fetch_dependencies import download, install_member


class DependencyTests(unittest.TestCase):
    def test_download_cache_and_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "file"
            digest = hashlib.sha256(b"good").hexdigest()
            with patch("emulator.fetch_dependencies.urlopen", return_value=io.BytesIO(b"good")) as fetch:
                download("https://example.invalid/file", path, digest)
                download("https://example.invalid/file", path, digest)
                fetch.assert_called_once()
            path.write_bytes(b"bad")
            with patch("emulator.fetch_dependencies.urlopen", return_value=io.BytesIO(b"wrong")):
                with self.assertRaises(ValueError):
                    download("https://example.invalid/file", path, digest)
            self.assertEqual(path.read_bytes(), b"bad")
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_archive_selective_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive = root / "archive.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                for name in ("../esp-emu", "nested/esp-emu"):
                    entry = tarfile.TarInfo(name)
                    entry.size = 3
                    tar.addfile(entry, io.BytesIO(b"bin"))
            output = root / "bin" / "esp-emu"
            install_member(archive, "esp-emu", output, 0o755)
            self.assertEqual(output.read_bytes(), b"bin")
            if os.name == 'posix':
                self.assertEqual(output.stat().st_mode & 0o777, 0o755)
            with self.assertRaises(ValueError):
                install_member(archive, "missing", output)


class SetupErrorTests(unittest.TestCase):
    def test_expected_setup_errors(self):
        from http.client import IncompleteRead
        from unittest.mock import patch
        from contextlib import redirect_stderr
        from io import StringIO
        from emulator.fetch_dependencies import main
        for error in (IncompleteRead(b"abc", 5), KeyError("micropython")):
            with self.subTest(error=error), patch("emulator.fetch_dependencies.setup", side_effect=error), redirect_stderr(StringIO()) as stderr:
                self.assertEqual(main(), 1)
                self.assertIn("Setup failed:", stderr.getvalue())
