import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from Zeropanel.minecraft import MinecraftError, console_output, create_backup, create_path, delete_backup, delete_path, list_backups, list_files, read_file, rename_path, restore_backup, runtime_metrics, update_server_settings, write_file


class MinecraftServiceTests(unittest.TestCase):
    def test_files_are_confined_and_editable_text_is_limited(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            server_root = Path(temp_dir) / "server"
            server = {"root_path": str(server_root), "disk_mb": 1}
            self.assertEqual(list_files(server), [])

            write_file(server, "server.properties", "motd=ZeroPanel\n")
            self.assertEqual(read_file(server, "server.properties"), "motd=ZeroPanel\n")
            self.assertEqual(list_files(server)[0]["name"], "server.properties")

            with self.assertRaisesRegex(MinecraftError, "invalid_server_file_path"):
                write_file(server, "../outside.txt", "no")
            with self.assertRaisesRegex(MinecraftError, "invalid_server_file_path"):
                read_file(server, "../../outside.txt")

    def test_disk_limit_blocks_a_file_that_exceeds_plan_allocation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            server = {"root_path": str(Path(temp_dir) / "server"), "disk_mb": 1}
            write_file(server, "world.dat", "x" * 900_000)
            with self.assertRaisesRegex(MinecraftError, "minecraft_disk_limit_reached"):
                write_file(server, "extra.dat", "x" * 200_000)

    def test_file_manager_create_rename_and_delete_stays_in_server_root(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            server = {"root_path": str(Path(temp_dir) / "server"), "disk_mb": 1}
            create_path(server, "plugins", "directory")
            create_path(server, "plugins/example.txt", "file")
            renamed = rename_path(server, "plugins/example.txt", "renamed.txt")
            self.assertEqual(renamed["name"], "renamed.txt")
            delete_path(server, "plugins/renamed.txt")
            self.assertEqual(list_files(server, "plugins"), [])
            with self.assertRaisesRegex(MinecraftError, "invalid_server_file_path"):
                create_path(server, "../outside.txt", "file")

    def test_stopped_console_does_not_require_docker(self):
        self.assertEqual(console_output({"status": "stopped", "id": 1}), "")

    def test_server_settings_enforce_valid_properties_without_changing_plan_limits(self):
        server = {"id": 1, "root_path": ".", "status": "stopped", "port": 25565, "ram_mb": 2048, "disk_mb": 1024, "settings_json": "{}"}
        result = update_server_settings(server, {
            "difficulty": "hard", "max_players": 60, "online_mode": True, "white_list": True,
            "allow_flight": False, "spawn_protection": 24, "ram_mb": 8192,
        })
        self.assertEqual(result["settings"]["difficulty"], "hard")
        self.assertEqual(result["settings"]["online_mode"], True)
        self.assertEqual(result["settings"]["spawn_protection"], 24)
        self.assertNotIn("ram_mb", result["settings"])
        with self.assertRaisesRegex(MinecraftError, "invalid_minecraft_numeric_setting"):
            update_server_settings(server, {"view_distance": "many"})

    def test_minecraft_backup_restore_and_delete(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "server"
            server = {"id": 3, "root_path": str(root), "status": "stopped", "disk_mb": 4}
            write_file(server, "server.properties", "motd=Original\n")
            backup = create_backup(server)
            self.assertEqual(list_backups(server)[0]["id"], backup["id"])
            write_file(server, "server.properties", "motd=Changed\n")
            write_file(server, "new-file.txt", "remove after restore")
            restored = restore_backup(server, backup["id"])
            self.assertTrue(restored["restored"])
            self.assertEqual(read_file(server, "server.properties"), "motd=Original\n")
            self.assertFalse((root / "new-file.txt").exists())
            delete_backup(server, backup["id"])
            self.assertEqual(list_backups(server), [])

    def test_running_minecraft_server_cannot_restore_backup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            server = {"id": 4, "root_path": str(Path(temp_dir) / "server"), "status": "running", "disk_mb": 4}
            write_file(server, "world.dat", "world")
            backup = create_backup({**server, "status": "stopped"})
            with self.assertRaisesRegex(MinecraftError, "minecraft_server_must_be_stopped"):
                restore_backup(server, backup["id"])

    def test_runtime_metrics_parse_real_docker_stats_output(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir) / "server"
            root.mkdir()
            (root / "world.dat").write_bytes(b"world")
            server = {"id": 5, "container_name": "mc-test", "root_path": str(root), "status": "running", "ram_mb": 8192, "disk_mb": 51200}
            stats = json.dumps({"CPUPerc": "23.4%", "MemUsage": "3.2GiB / 8GiB", "NetIO": "2.4MB / 840KB"})

            def docker_result(args, **kwargs):
                output = stats if args[0] == "stats" else "2026-09-26T00:00:00Z"
                return SimpleNamespace(stdout=output)

            with mock.patch("Zeropanel.minecraft._container_exists", return_value=True), mock.patch("Zeropanel.minecraft._docker", side_effect=docker_result):
                metrics = runtime_metrics(server)

            self.assertEqual(metrics["cpu_percent"], 23.4)
            self.assertEqual(metrics["ram_used_bytes"], int(3.2 * 1024 ** 3))
            self.assertEqual(metrics["network_rx_bytes"], 2 * 1024 ** 2 + int(.4 * 1024 ** 2))
            self.assertEqual(metrics["network_tx_bytes"], 840 * 1024)
            self.assertEqual(metrics["disk_used_bytes"], 5)
            self.assertIsNotNone(metrics["uptime_seconds"])


if __name__ == "__main__":
    unittest.main()