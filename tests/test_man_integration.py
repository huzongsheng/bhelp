from __future__ import annotations

import os
import shlex
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bhelp.config import initialize_config, load_config, write_config
from bhelp.integration import (
    GitIntegrationStatus,
    IntegrationError,
    install_man_integration,
    man_status,
    migrate_legacy_git_integration,
    remove_man_integration,
)


class ManIntegrationTests(unittest.TestCase):
    def paths(self, directory: str) -> tuple[Path, Path, Path]:
        root = Path(directory)
        return root / ".bashrc", root / "config.json", root / "bin dir" / "bhelp's executable"

    def test_install_is_idempotent_shell_quotes_absolute_path_and_backs_up_rc(self):
        with tempfile.TemporaryDirectory() as directory:
            rc_path, config_path, executable = self.paths(directory)
            original = "export PAGER='less -R'\nalias ll='ls -l'\n"
            rc_path.write_text(original, encoding="utf-8")
            initialize_config(config_path)

            installed = install_man_integration(executable, config_path, rc_path=rc_path)
            first_result = rc_path.read_text(encoding="utf-8")
            expected_command = (
                f"{shlex.quote(str(executable.resolve()))} pager --config "
                f"{shlex.quote(str(config_path.resolve()))}"
            )
            self.assertTrue(installed.installed)
            self.assertEqual(installed.command, expected_command)
            self.assertIn(f"export MANPAGER={shlex.quote(expected_command)}", first_result)
            self.assertEqual(first_result.count("# >>> bhelp MANPAGER integration >>>"), 1)
            self.assertIn("export PAGER='less -R'", first_result)
            self.assertNotIn("\nexport PAGER=", first_result[len(original) :])
            self.assertIsNotNone(installed.backup_path)
            backup = Path(installed.backup_path or "")
            self.assertEqual(backup.read_text(encoding="utf-8"), original)
            if os.name == "posix":
                self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

            installed_again = install_man_integration(executable, config_path, rc_path=rc_path)
            self.assertTrue(installed_again.installed)
            self.assertEqual(rc_path.read_text(encoding="utf-8"), first_result)
            self.assertEqual(installed_again.backup_path, installed.backup_path)

    @unittest.skipUnless(os.name == "posix", "backup permissions are POSIX-specific")
    def test_matching_existing_backup_is_resecured_before_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            rc_path, config_path, executable = self.paths(directory)
            original = "alias safe='true'\n"
            rc_path.write_text(original, encoding="utf-8")
            backup = rc_path.with_name(f"{rc_path.name}.bhelp-backup")
            backup.write_text(original, encoding="utf-8")
            backup.chmod(0o644)
            initialize_config(config_path)

            installed = install_man_integration(executable, config_path, rc_path=rc_path)

            self.assertEqual(Path(installed.backup_path or ""), backup)
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

    def test_default_config_does_not_add_a_config_argument(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "HOME": directory,
                "XDG_CONFIG_HOME": str(Path(directory) / ".config"),
            },
            clear=False,
        ):
            rc_path = Path(directory) / ".bashrc"
            executable = Path(directory) / "bhelp"

            installed = install_man_integration(executable, rc_path=rc_path)
            self.assertEqual(installed.command, f"{shlex.quote(str(executable.resolve()))} pager")

    def test_environment_config_is_carried_into_manpager_command(self):
        with tempfile.TemporaryDirectory() as directory:
            rc_path = Path(directory) / ".bashrc"
            config_path = Path(directory) / "environment-config.json"
            executable = Path(directory) / "bhelp"
            initialize_config(config_path)
            with patch.dict(os.environ, {"BHELP_CONFIG": str(config_path)}, clear=False):
                installed = install_man_integration(executable, rc_path=rc_path)

            expected = (
                f"{shlex.quote(str(executable.resolve()))} pager --config "
                f"{shlex.quote(str(config_path.resolve()))}"
            )
            self.assertEqual(installed.command, expected)
            self.assertIn(
                f"export MANPAGER={shlex.quote(expected)}",
                rc_path.read_text(encoding="utf-8"),
            )

    def test_explicit_config_is_carried_into_manpager_command(self):
        with tempfile.TemporaryDirectory() as directory:
            rc_path, config_path, executable = self.paths(directory)
            initialize_config(config_path)

            installed = install_man_integration(executable, config_path, rc_path=rc_path)
            expected_command = (
                f"{shlex.quote(str(executable.resolve()))} pager --config "
                f"{shlex.quote(str(config_path.resolve()))}"
            )
            self.assertEqual(installed.command, expected_command)
            self.assertIn(
                f"export MANPAGER={shlex.quote(expected_command)}",
                rc_path.read_text(encoding="utf-8"),
            )

    @unittest.skipUnless(os.name == "posix", "symbolic-link behavior is POSIX-specific")
    def test_install_refuses_symbolic_link_rc_file(self):
        with tempfile.TemporaryDirectory() as directory:
            rc_path, config_path, executable = self.paths(directory)
            target = Path(directory) / "real-bashrc"
            target.write_text("alias safe='true'\n", encoding="utf-8")
            rc_path.symlink_to(target)
            initialize_config(config_path)

            with self.assertRaisesRegex(IntegrationError, "symbolic-link shell rc"):
                install_man_integration(executable, config_path, rc_path=rc_path)
            self.assertEqual(target.read_text(encoding="utf-8"), "alias safe='true'\n")

    @unittest.skipUnless(os.name == "posix", "sourcing .bashrc requires a POSIX shell")
    def test_bash_sources_shell_quoted_manpager_as_one_command(self):
        with tempfile.TemporaryDirectory() as directory:
            rc_path, config_path, executable = self.paths(directory)
            initialize_config(config_path)
            install_man_integration(executable, config_path, rc_path=rc_path)

            result = subprocess.run(
                ["bash", "-c", 'source "$1"; printf "%s" "$MANPAGER"', "_", str(rc_path)],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                result.stdout,
                f"{shlex.quote(str(executable.resolve()))} pager --config "
                f"{shlex.quote(str(config_path.resolve()))}",
            )

    def test_remove_restores_original_and_preserves_later_user_edits(self):
        with tempfile.TemporaryDirectory() as directory:
            rc_path, config_path, executable = self.paths(directory)
            original = "alias before='true'\n"
            rc_path.write_text(original, encoding="utf-8")
            initialize_config(config_path)
            install_man_integration(executable, config_path, rc_path=rc_path)
            with rc_path.open("a", encoding="utf-8") as handle:
                handle.write("alias after='true'\n")

            removed = remove_man_integration(config_path, rc_path=rc_path)
            self.assertFalse(removed.installed)
            self.assertFalse(removed.modified)
            self.assertEqual(rc_path.read_text(encoding="utf-8"), original + "alias after='true'\n")
            self.assertNotIn("MANPAGER", rc_path.read_text(encoding="utf-8"))

            removed_again = remove_man_integration(config_path, rc_path=rc_path)
            self.assertFalse(removed_again.installed)
            self.assertEqual(rc_path.read_text(encoding="utf-8"), original + "alias after='true'\n")

    def test_remove_deletes_rc_that_install_created_from_scratch(self):
        with tempfile.TemporaryDirectory() as directory:
            rc_path, config_path, executable = self.paths(directory)
            initialize_config(config_path)
            install_man_integration(executable, config_path, rc_path=rc_path)
            self.assertTrue(rc_path.exists())

            remove_man_integration(config_path, rc_path=rc_path)
            self.assertFalse(rc_path.exists())

    def test_remove_refuses_a_user_modified_managed_block(self):
        with tempfile.TemporaryDirectory() as directory:
            rc_path, config_path, executable = self.paths(directory)
            rc_path.write_text("alias safe='true'\n", encoding="utf-8")
            initialize_config(config_path)
            install_man_integration(executable, config_path, rc_path=rc_path)
            modified = rc_path.read_text(encoding="utf-8").replace(
                "export MANPAGER=", "export MANPAGER=custom # was "
            )
            rc_path.write_text(modified, encoding="utf-8")

            status = man_status(config_path, rc_path=rc_path)
            self.assertFalse(status.installed)
            self.assertTrue(status.modified)
            with self.assertRaisesRegex(IntegrationError, "modified"):
                remove_man_integration(config_path, rc_path=rc_path)
            self.assertEqual(rc_path.read_text(encoding="utf-8"), modified)
            self.assertIn("man", load_config(config_path).get("integration_state", {}))

    def test_foreign_or_orphaned_markers_are_never_overwritten_or_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            rc_path, config_path, executable = self.paths(directory)
            rc_path.write_text(
                "# >>> bhelp MANPAGER integration >>>\nexport MANPAGER=custom\n"
                "# <<< bhelp MANPAGER integration <<<\n",
                encoding="utf-8",
            )
            initialize_config(config_path)

            self.assertTrue(man_status(config_path, rc_path=rc_path).modified)
            with self.assertRaisesRegex(IntegrationError, "refusing"):
                install_man_integration(executable, config_path, rc_path=rc_path)
            with self.assertRaisesRegex(IntegrationError, "refusing"):
                remove_man_integration(config_path, rc_path=rc_path)


class LegacyGitMigrationTests(unittest.TestCase):
    command = "/home/user/.local/bin/bhelp man"

    def write_legacy_state(self, config_path: Path) -> None:
        initialize_config(config_path)
        config = load_config(config_path)
        config["integration_state"] = {
            "git": {
                "previous_man_viewer": ["man"],
                "previous_man_bhelp_cmd": "user-viewer --flag",
                "previous_help_format": ["web"],
                "installed_man_bhelp_cmd": self.command,
            }
        }
        write_config(config, config_path)

    def test_owned_legacy_git_viewer_is_restored_and_state_is_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            self.write_legacy_state(config_path)
            current = GitIntegrationStatus(True, ["bhelp"], self.command, ["man"])
            with patch("bhelp.integration.git_status", return_value=current), patch(
                "bhelp.integration._replace_all"
            ) as replace_all, patch("bhelp.integration._git") as git:
                migrated = migrate_legacy_git_integration(config_path)

            self.assertTrue(migrated)
            self.assertEqual(
                replace_all.call_args_list,
                [
                    unittest.mock.call("man.viewer", ["man"]),
                    unittest.mock.call("help.format", ["web"]),
                ],
            )
            git.assert_called_once_with(
                ["config", "--global", "man.bhelp.cmd", "user-viewer --flag"]
            )
            self.assertNotIn("integration_state", load_config(config_path))

    def test_customized_git_settings_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            self.write_legacy_state(config_path)
            customized = GitIntegrationStatus(
                False,
                ["bhelp", "user-custom-viewer"],
                self.command,
                ["man"],
            )
            with patch("bhelp.integration.git_status", return_value=customized), patch(
                "bhelp.integration._replace_all"
            ) as replace_all, patch("bhelp.integration._git") as git:
                migrated = migrate_legacy_git_integration(config_path)

            self.assertFalse(migrated)
            replace_all.assert_not_called()
            git.assert_not_called()
            self.assertIn("git", load_config(config_path)["integration_state"])

    def test_install_runs_safe_legacy_migration_after_writing_man_state(self):
        with tempfile.TemporaryDirectory() as directory:
            rc_path = Path(directory) / ".bashrc"
            config_path = Path(directory) / "config.json"
            executable = Path(directory) / "bhelp"
            self.write_legacy_state(config_path)
            current = GitIntegrationStatus(True, ["bhelp"], self.command, ["man"])
            with patch("bhelp.integration.git_status", return_value=current), patch(
                "bhelp.integration._replace_all"
            ), patch("bhelp.integration._git"):
                installed = install_man_integration(executable, config_path, rc_path=rc_path)

            self.assertTrue(installed.installed)
            self.assertTrue(installed.migrated_legacy_git)
            state = load_config(config_path)["integration_state"]
            self.assertIn("man", state)
            self.assertNotIn("git", state)


if __name__ == "__main__":
    unittest.main()
