import os
import copy
import subprocess
import tempfile
import unittest
import zipapp
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from bhelp.cli import _handle_doctor, build_parser
from bhelp.config import DEFAULT_CONFIG, initialize_config
from bhelp.integration import git_status, install_git_integration, remove_git_integration
from bhelp.provider import ProviderError


class ParserTests(unittest.TestCase):
    def test_common_options_work_before_and_after_subcommand(self):
        parser = build_parser()
        before = parser.parse_args(["--to", "ja", "pipe"])
        after = parser.parse_args(["pipe", "--to", "ja"])
        self.assertEqual(before.target_language, "ja")
        self.assertEqual(after.target_language, "ja")

    def test_run_keeps_command_options_after_separator(self):
        args = build_parser().parse_args(["run", "--", "git", "clone", "--help"])
        self.assertEqual(args.command, ["--", "git", "clone", "--help"])

    def test_zipapp_preserves_wrapped_command_exit_code(self):
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "bhelp"
            zipapp.create_archive(
                project_root / "src",
                target=archive,
                interpreter="/usr/bin/env python3",
                main="bhelp.cli:entrypoint",
            )
            result = subprocess.run(
                [str(archive), "run", "--no-network", "--pager", "never", "--", "sh", "-c", "exit 7"],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 7)

    def test_doctor_no_network_refuses_before_provider_call(self):
        args = Namespace(no_network=True)
        with self.assertRaisesRegex(ProviderError, "requires network"):
            _handle_doctor(args, copy.deepcopy(DEFAULT_CONFIG))


@unittest.skipUnless(os.name == "posix", "Git integration behavior is Linux-specific")
class GitIntegrationTests(unittest.TestCase):
    def test_install_and_remove_restores_previous_viewer(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"HOME": directory, "XDG_CONFIG_HOME": os.path.join(directory, ".config")},
            clear=False,
        ):
            subprocess.run(
                ["git", "config", "--global", "man.viewer", "man"], check=True, capture_output=True
            )
            subprocess.run(
                ["git", "config", "--global", "help.format", "web"], check=True, capture_output=True
            )
            initialize_config()
            installed = install_git_integration()
            self.assertTrue(installed.installed)
            self.assertEqual(git_status().viewers, ["bhelp"])
            self.assertEqual(git_status().help_formats, ["man"])
            removed = remove_git_integration()
            self.assertFalse(removed.installed)
            self.assertEqual(removed.viewers, ["man"])
            self.assertEqual(removed.help_formats, ["web"])

    def test_absolute_viewer_with_explicit_config_is_recognized(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"HOME": directory, "XDG_CONFIG_HOME": os.path.join(directory, ".config")},
            clear=False,
        ):
            initialize_config()
            command = "/home/user/.local/bin/bhelp man --config /tmp/bhelp-config.json"
            installed = install_git_integration(viewer_command=command)
            self.assertTrue(installed.installed)
            self.assertEqual(installed.command, command)
            removed = remove_git_integration()
            self.assertFalse(removed.installed)


if __name__ == "__main__":
    unittest.main()
