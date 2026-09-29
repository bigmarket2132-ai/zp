import tempfile
import sqlite3
import unittest
from pathlib import Path

from Zeropanel.db import SCHEMA, ensure_schema
from Zeropanel.store import StoreError, adjust_wallet, approve_order, place_order, renew_due_orders, save_category, save_plan


class StoreBillingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        ensure_schema(self.conn)
        self.addCleanup(self.conn.close)
        self.user_id = self.conn.execute(
            "INSERT INTO users(email, password_hash, full_name) VALUES (?, ?, ?)",
            ("customer@example.test", "test-hash", "Customer"),
        ).lastrowid
        self.admin_id = self.conn.execute(
            "INSERT INTO admins(email, password_hash, full_name) VALUES (?, ?, ?)",
            ("admin@example.test", "test-hash", "Admin"),
        ).lastrowid
        self.hosting_plan_id = self.conn.execute(
            """INSERT INTO plans(name, cpu_limit, memory_mb, storage_mb, inode_limit, max_websites,
               max_databases, max_mailboxes, max_cron_jobs, daily_email_limit, backup_retention_days)
               VALUES ('Website Small', '1', 1024, 10240, 10000, 1, 3, 2, 0, 0, 7)"""
        ).lastrowid
        self.node_id = self.conn.execute(
            "INSERT INTO nodes(name, hostname, status) VALUES ('local', 'localhost', 'online')"
        ).lastrowid
        self.category_id = save_category(self.conn, {"name": "Web hosting"})["id"]

    def test_pending_order_is_debited_and_fulfilled_only_once_after_approval(self):
        plan = save_plan(self.conn, {
            "category_id": self.category_id,
            "name": "Website Small",
            "product_type": "hosting",
            "hosting_plan_id": self.hosting_plan_id,
            "price_cents": 1200,
        })
        order = place_order(self.conn, self.user_id, plan["id"])
        self.assertEqual(order["status"], "pending")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM wallet_transactions").fetchone()[0], 0)

        adjust_wallet(self.conn, self.user_id, 2000, self.admin_id, "Credit added")
        delivered = approve_order(self.conn, order["id"], self.admin_id, Path(self.temp.name) / "accounts")
        self.assertEqual(delivered["status"], "approved")
        self.assertEqual(delivered["product_type"], "hosting")
        self.assertTrue(self.conn.execute("SELECT 1 FROM hosting_accounts WHERE id = ?", (delivered["service_id"],)).fetchone())
        self.assertEqual(self.conn.execute("SELECT balance_cents FROM wallets WHERE user_id = ?", (self.user_id,)).fetchone()[0], 800)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM wallet_transactions WHERE order_id = ?", (order["id"],)).fetchone()[0], 1)

        with self.assertRaisesRegex(StoreError, "order_not_pending"):
            approve_order(self.conn, order["id"], self.admin_id, Path(self.temp.name) / "accounts")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM wallet_transactions WHERE order_id = ?", (order["id"],)).fetchone()[0], 1)

    def test_insufficient_credit_keeps_order_pending(self):
        plan = save_plan(self.conn, {
            "category_id": self.category_id,
            "name": "Website Pro",
            "product_type": "hosting",
            "hosting_plan_id": self.hosting_plan_id,
            "price_cents": 1200,
        })
        order = place_order(self.conn, self.user_id, plan["id"])
        with self.assertRaisesRegex(StoreError, "insufficient_balance"):
            approve_order(self.conn, order["id"], self.admin_id, Path(self.temp.name) / "accounts")
        self.assertEqual(self.conn.execute("SELECT status FROM store_orders WHERE id = ?", (order["id"],)).fetchone()[0], "pending")

    def test_minecraft_order_captures_plan_limits(self):
        plan = save_plan(self.conn, {
            "category_id": self.category_id,
            "name": "Minecraft 4 GB",
            "product_type": "minecraft",
            "price_cents": 500,
            "limits": {"ram_mb": 4096, "disk_mb": 20480, "max_servers": 1},
            "server_types": ["PAPER", "VANILLA"],
        })
        order = place_order(self.conn, self.user_id, plan["id"])
        adjust_wallet(self.conn, self.user_id, 500, self.admin_id, "Credit added")
        approved = approve_order(self.conn, order["id"], self.admin_id, Path(self.temp.name) / "accounts")
        server = self.conn.execute("SELECT * FROM minecraft_servers WHERE id = ?", (approved["service_id"],)).fetchone()
        self.assertEqual(server["ram_mb"], 4096)
        self.assertEqual(server["server_type"], "PAPER")
        self.assertEqual(server["status"], "stopped")

    def test_monthly_hosting_renews_or_suspends_from_wallet_balance(self):
        plan = save_plan(self.conn, {
            "category_id": self.category_id,
            "name": "Monthly Website",
            "product_type": "hosting",
            "hosting_plan_id": self.hosting_plan_id,
            "price_cents": 900,
            "billing_interval": "month",
        })
        order = place_order(self.conn, self.user_id, plan["id"])
        adjust_wallet(self.conn, self.user_id, 900, self.admin_id, "Initial credit")
        approved = approve_order(self.conn, order["id"], self.admin_id, Path(self.temp.name) / "accounts")
        self.conn.execute("UPDATE store_orders SET renewal_at = datetime('now', '-1 day') WHERE id = ?", (order["id"],))

        self.assertEqual(renew_due_orders(self.conn), 1)
        self.assertEqual(self.conn.execute("SELECT status FROM store_orders WHERE id = ?", (order["id"],)).fetchone()[0], "suspended")
        self.assertEqual(self.conn.execute("SELECT status FROM hosting_accounts WHERE id = ?", (approved["service_id"],)).fetchone()[0], "suspended")

        adjust_wallet(self.conn, self.user_id, 900, self.admin_id, "Renewal credit")
        self.assertEqual(renew_due_orders(self.conn), 1)
        self.assertEqual(self.conn.execute("SELECT status FROM store_orders WHERE id = ?", (order["id"],)).fetchone()[0], "approved")
        self.assertEqual(self.conn.execute("SELECT status FROM hosting_accounts WHERE id = ?", (approved["service_id"],)).fetchone()[0], "active")
        self.assertEqual(self.conn.execute("SELECT balance_cents FROM wallets WHERE user_id = ?", (self.user_id,)).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()