import argparse
from contextlib import nullcontext, redirect_stderr
from io import StringIO
import hashlib
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from emulator import cli


class CliTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.build = self.root / "firmware-build"
        self.emulator = self.root / "emulator"
        self.build.mkdir()
        self.emulator.mkdir()
        for name, value in (("ROOT", self.root), ("BUILD", self.build),
                            ("EMULATOR", self.emulator), ("LOCK", self.emulator / ".build.lock")):
            patcher = patch.object(cli, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.parser = argparse.ArgumentParser()
        cli.add_commands(self.parser.add_subparsers(required=True))

    def mock_host(self):
        # Exercise orchestration separately from the Linux flock/UID boundary.
        self.assertFalse(cli.LOCK.exists())
        cli.LOCK.touch()
        for patcher in (patch.object(cli, 'locked', side_effect=lambda timeout: nullcontext(9)),
                        patch.object(os, 'getuid', return_value=1000, create=True),
                        patch.object(os, 'getgid', return_value=1000, create=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def parse(self, *args):
        return self.parser.parse_args(args)

    def artifacts(self, version="2026", heap=True):
        records = []
        for name, size in (("micropython.bin", 64), ("vfs.bin", 0x200000),
                           (cli.FIRMWARE, 0x400000)):
            data = bytes(size)
            (self.build / name).write_bytes(data)
            records.append(f"{hashlib.sha256(data).hexdigest()}  {name}\n")
        (self.build / "SHA256SUMS").write_text("".join(records))
        (self.build / "badge-version").write_text(f"{version}:{int(heap)}\n")
        (self.build / "input.sha256").write_text("a"*64)
        (self.build / "build-profile.sha256").write_text(cli.profile_digest(version, heap, "a"*64))
        (self.emulator / cli.BOARDS[version]).write_text("{}")
        return {"micropython": {"sha256": hashlib.sha256(bytes(64)).hexdigest()}}

    def test_commands_and_defaults(self):
        args = self.parse("emulator", "build")
        self.assertEqual(args.badge_version, "2025")
        self.assertFalse(args.guest_heap)
        self.assertFalse(args.force)
        self.assertIs(args.handler, cli.build)
        self.assertIs(self.parse("emulator", "run").handler, cli.run)
        self.assertIs(self.parse("test").handler, cli.tests)
        for name in ("start", "setup", "verify", "probe", "lock", "test"):
            with self.subTest(name=name), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                self.parse("emulator", name)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux file locking")
    def test_lock_timeout_and_reuse(self):
        with cli.locked(0):
            with self.assertRaisesRegex(cli.EmulatorError, "lock busy"):
                with cli.locked(0):
                    self.fail("unexpected second owner")
        with cli.locked(0):
            pass
        self.assertTrue(cli.LOCK.exists())

    def test_profile_binding(self):
        self.artifacts()
        self.assertEqual(cli.profile(), ("2026", True))
        (self.build / "badge-version").write_text("2025:0")
        with self.assertRaisesRegex(cli.EmulatorError, "metadata"):
            cli.profile()

    def test_artifact_corruption(self):
        data = self.artifacts()
        with patch.object(cli, "manifest", return_value=data):
            cli.verify_artifacts()
            (self.build / cli.FIRMWARE).write_bytes(b"bad")
            with self.assertRaises(cli.EmulatorError):
                cli.verify_artifacts()

    def test_manifest_rejects_traversal_before_reading_outside_build(self):
        data = self.artifacts()
        outside = self.root / "outside"
        outside.write_bytes(b"valid external content")
        manifest_path = self.build / "SHA256SUMS"
        digest = hashlib.sha256(outside.read_bytes()).hexdigest()
        with patch.object(cli, "manifest", return_value=data):
            cli.verify_artifacts()
            with manifest_path.open("a", encoding="ascii") as stream:
                stream.write(f"{digest}  ../outside\n")
            with patch.object(cli.dependencies, "sha256", wraps=cli.dependencies.sha256) as hash_file:
                with self.assertRaisesRegex(cli.EmulatorError, "Invalid artifact manifest entry"):
                    cli.verify_artifacts()
                hash_file.assert_not_called()

    def test_run_uses_existing_profile_without_docker_or_restore(self):
        self.mock_host()
        data = self.artifacts()
        with patch.object(cli, "manifest", return_value=data), patch.object(cli, "verify_dependencies"), patch.object(cli, "launch") as launch, patch.object(cli, "child") as child:
            cli.run(self.parse("emulator", "run", "--cosim-log"))
        child.assert_not_called()
        command, heap, fd = launch.call_args.args
        self.assertTrue(heap)
        self.assertIn(str(self.emulator / "board-2026.json"), command)
        self.assertIn("--cosim-log", command)

    def test_run_missing_artifact_fails_before_launch(self):
        self.mock_host()
        with patch.object(cli, "launch") as launch, self.assertRaises(cli.EmulatorError):
            cli.run(self.parse("emulator", "run"))
        launch.assert_not_called()

    def test_build_reuse_and_force(self):
        self.mock_host()
        data = self.artifacts("2025", False)
        for name in ("Dockerfile", "requirements-build.lock", "requirements.lock"):
            (self.emulator / name).write_text(name)
        (self.build / "builder.identity").write_text(cli.builder_identity("sha256:image"))
        with patch.object(cli, "manifest", return_value=data), patch.object(cli, "verify_dependencies"), patch.object(cli.firmware_inputs, "fingerprint", return_value="a"*64), patch.object(cli, "child", return_value=Mock(stdout="sha256:image")) as child:
            cli.build(self.parse("emulator", "build"))
            self.assertEqual(child.call_count, 1)
            child.reset_mock()
            cli.build(self.parse("emulator", "build", "--force"))
            self.assertEqual(child.call_count, 2)
            command = child.call_args.args[0]
            self.assertEqual(command[:2], ["docker", "run"])
            self.assertIn("never", command)
            self.assertIn("none", command)
        self.assertEqual(cli.profile(), ("2025", False))

    def test_clean_preserves_restored_dependencies(self):
        self.mock_host()
        self.artifacts()
        (self.build / "builder.identity").write_text("builder")
        cli.clean(self.parse("emulator", "clean"))
        self.assertTrue((self.build / "micropython.bin").exists())
        self.assertTrue((self.build / "builder.identity").exists())
        self.assertFalse((self.build / cli.FIRMWARE).exists())
        self.assertTrue(cli.LOCK.exists())

    def test_client_forwards_native_options(self):
        with patch.object(cli, "require_linux"), patch.object(cli, "child") as child:
            cli.client(self.parse("emulator", "controller", "--guest-heap", "--refresh-hz", "100", "--led-counterclockwise"))
        command = child.call_args.args[0]
        self.assertIn("emulator.controller", command)
        self.assertIn(str(self.build / "guest-heap.json"), command)
        self.assertIn("100.0", command)
        self.assertIn("--led-counterclockwise", command)

    def test_all_suites_run_even_if_first_fails(self):
        with patch.object(cli.subprocess, "run", side_effect=[Mock(returncode=1), Mock(returncode=0)]) as run:
            with self.assertRaises(cli.EmulatorError):
                cli.tests(self.parse("test"))
        self.assertEqual(run.call_count, 2)

    def test_restore_records_builder_identity(self):
        self.mock_host()
        for name in ("Dockerfile", "requirements-build.lock", "requirements.lock"):
            (self.emulator / name).write_text(name)
        with patch.object(cli, "child", return_value=Mock(stdout="sha256:image")) as child:
            cli.restore(self.parse("emulator", "restore"))
        commands = [entry.args[0] for entry in child.call_args_list]
        self.assertEqual(commands[0], [sys.executable, "-m", "emulator.fetch_dependencies"])
        self.assertEqual(commands[1][:2], ["docker", "build"])
        self.assertEqual(commands[2][:3], ["docker", "image", "inspect"])
        self.assertEqual((self.build / "builder.identity").read_text().strip(),
                         cli.builder_identity("sha256:image"))

    def test_failed_build_removes_container_and_invalidates_profile(self):
        self.mock_host()
        import subprocess
        data = self.artifacts("2025", False)
        for name in ("Dockerfile", "requirements-build.lock", "requirements.lock"):
            (self.emulator / name).write_text(name)
        (self.build / "builder.identity").write_text(cli.builder_identity("sha256:image"))
        with patch.object(cli, "manifest", return_value=data), patch.object(cli, "verify_dependencies"), patch.object(cli.firmware_inputs, "fingerprint", return_value="a"*64), patch.object(cli, "child", side_effect=[Mock(stdout="sha256:image"), subprocess.CalledProcessError(1, "docker")]) as child, patch.object(cli.subprocess, "run") as cleanup:
            with self.assertRaises(subprocess.CalledProcessError):
                cli.build(self.parse("emulator", "build", "--force"))
        command = child.call_args.args[0]
        cleanup.assert_called_once_with(["docker", "rm", "-f", command[command.index("--name")+1]],
                                        cwd=self.root, check=False)
        self.assertFalse((self.build / "build-profile.sha256").exists())

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux process supervision")
    def test_launch_propagates_exit_and_releases_lock(self):
        import subprocess
        with cli.locked(0) as fd:
            cli.launch([sys.executable, "-c", "pass"], False, fd)
        with self.assertRaises(subprocess.CalledProcessError) as failure:
            with cli.locked(0) as fd:
                cli.launch([sys.executable, "-c", "raise SystemExit(7)"], False, fd)
        self.assertEqual(failure.exception.returncode, 7)
        with cli.locked(0):
            pass
