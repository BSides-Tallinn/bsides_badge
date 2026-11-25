import tempfile
import unittest
from pathlib import Path
from emulator.firmware_inputs import stage, staged_config


class InputTests(unittest.TestCase):
    def test_stage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            output.mkdir()
            for name in ("main.py", "badge.json", "params.json", "id.txt", ".secret.py", "debug.log"):
                (source / name).write_text("local")
            (source/"nested").mkdir()
            (source/"nested/asset.py").write_text("nested asset")
            stage(source, output, "2026")
            self.assertEqual({p.name for p in output.iterdir()}, {"main.py", "badge.json", "nested"})
            self.assertEqual((output/"nested/asset.py").read_text(), "nested asset")
            self.assertEqual((output / "badge.json").read_bytes(), b'{"badge_version": "2026", "device_id": "", "git_commit": "unknown", "holder_name": ""}\n')

    def test_invalid_version(self):
        with self.assertRaises(ValueError):
            staged_config("unknown")

    def test_unknown_asset_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root/"src", root/"out"
            source.mkdir()
            output.mkdir()
            (source/"sprite.bin").write_bytes(b"asset")
            with self.assertRaisesRegex(ValueError, "Unknown firmware asset"):
                stage(source, output, "2025")

    def test_fingerprint_inputs(self):
        from unittest.mock import patch
        from emulator.firmware_inputs import fingerprint
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            files = ("software/main.py", "emulator/Dockerfile", "emulator/build_firmware.sh",
                     "emulator/firmware_inputs.py", "emulator/dependencies.json",
                     "emulator/requirements.lock", "emulator/requirements-build.lock",
                     "emulator/validate_firmware.py", "firmware-build/micropython.bin",
                     "emulator/guest_heap.py")
            for name in files:
                path = root/name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"original")
            with patch("emulator.firmware_inputs.ROOT", root):
                baseline = fingerprint("2025", "image", True)
                self.assertNotEqual(baseline, fingerprint("2026", "image", True))
                self.assertNotEqual(baseline, fingerprint("2025", "other", True))
                self.assertNotEqual(baseline, fingerprint("2025", "image", False))
                for name in files:
                    with self.subTest(name=name):
                        (root/name).write_bytes(b"changed")
                        self.assertNotEqual(baseline, fingerprint("2025", "image", True))
                        (root/name).write_bytes(b"original")
                        self.assertEqual(baseline, fingerprint("2025", "image", True))

    def test_instrumented_stage(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, output = root/"src", root/"out"
            source.mkdir()
            output.mkdir()
            (root/"emulator").mkdir()
            (root/"emulator/guest_heap.py").write_text("probe")
            original = "async def run():\n    await asyncio.gather(*tasks)\n"
            (source/"bsides.py").write_text(original)
            with patch("emulator.firmware_inputs.ROOT", root):
                stage(source, output, "2025", True)
            self.assertIn("tasks.append(sample_heap())", (output/"bsides.py").read_text())
            self.assertEqual((output/"guest_heap.py").read_text(), "probe")
            self.assertEqual((source/"bsides.py").read_text(), original)

    def test_symlink_rejected(self):
        from emulator.firmware_inputs import source_files
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            try:
                (root/"link.py").symlink_to(root/"missing")
            except OSError as exc:
                if getattr(exc, "winerror", None) == 1314:
                    self.skipTest("Host does not permit symbolic link creation")
                raise
            with self.assertRaisesRegex(ValueError, "Symlink"):
                list(source_files(root))
