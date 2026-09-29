import json
import re

from .db import create_job, row_to_dict


class StoreError(ValueError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code = code
        self.status = status


def _json(value, fallback):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "")
        return parsed if isinstance(parsed, dict) else fallback
    except (TypeError, ValueError):
        return fallback


def _int(value, field, minimum=0, maximum=2_147_483_647):
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise StoreError(f"invalid_{field}")
    if result < minimum or result > maximum:
        raise StoreError(f"invalid_{field}")
    return result


def _name(value, field, maximum=80):
    result = str(value or "").strip()
    if not result or len(result) > maximum:
        raise StoreError(f"invalid_{field}")
    return result


def category_payload(row):
    result = row_to_dict(row)
    if result.get("image_url"):
        result["image_url"] = result["image_url"]
    return result


def plan_payload(row):
    result = row_to_dict(row)
    result["limits"] = _json(result.pop("limits_json", "{}"), {})
    result["features"] = _json(result.pop("features_json", "{}"), {})
    if result.get("image_url"):
        result["image_url"] = result["image_url"]
    return result


def catalog(conn):
    categories = conn.execute(
        "SELECT * FROM store_categories WHERE enabled = 1 ORDER BY sort_order, name"
    ).fetchall()
    result = []
    for category in categories:
        plans = conn.execute(
            "SELECT * FROM store_plans WHERE category_id = ? AND enabled = 1 ORDER BY id",
            (category["id"],),
        ).fetchall()
        products = []
        for plan in plans:
            product = plan_payload(plan)
            if product["product_type"] == "hosting":
                hosting = conn.execute(
                    "SELECT name, memory_mb, storage_mb, max_websites, max_databases, max_mailboxes FROM plans WHERE id = ?",
                    (product["hosting_plan_id"],),
                ).fetchone()
                product["hosting_limits"] = row_to_dict(hosting) if hosting else {
                    "name": "", "memory_mb": 0, "storage_mb": 0, "max_websites": 0,
                    "max_databases": 0, "max_mailboxes": 0,
                }
            products.append(product)
        result.append({
            "category": category_payload(category),
            "plans": products,
        })
    return result


def wallet(conn, user_id):
    conn.execute("INSERT OR IGNORE INTO wallets(user_id, balance_cents) VALUES (?, 0)", (user_id,))
    balance = conn.execute("SELECT balance_cents FROM wallets WHERE user_id = ?", (user_id,)).fetchone()
    return int(balance["balance_cents"])


def adjust_wallet(conn, user_id, delta_cents, admin_id, description):
    delta_cents = _int(delta_cents, "wallet_adjustment", minimum=-2_147_483_647, maximum=2_147_483_647)
    if delta_cents == 0:
        raise StoreError("wallet_adjustment_must_not_be_zero")
    if not conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone():
        raise StoreError("customer_not_found", 404)
    conn.execute("INSERT OR IGNORE INTO wallets(user_id, balance_cents) VALUES (?, 0)", (user_id,))
    updated = conn.execute(
        "UPDATE wallets SET balance_cents = balance_cents + ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND balance_cents + ? >= 0",
        (delta_cents, user_id, delta_cents),
    )
    if updated.rowcount != 1:
        raise StoreError("insufficient_balance", 409)
    reference = f"admin:{admin_id}:{user_id}:{conn.execute('SELECT COALESCE(MAX(id), 0) + 1 AS next_id FROM wallet_transactions').fetchone()['next_id']}"
    conn.execute(
        "INSERT INTO wallet_transactions(user_id, admin_id, delta_cents, reference, description) VALUES (?, ?, ?, ?, ?)",
        (user_id, admin_id, delta_cents, reference, str(description or "Admin balance adjustment").strip()[:240]),
    )
    return wallet(conn, user_id)


def save_category(conn, payload, category_id=None):
    name = _name(payload.get("name"), "category_name", 48)
    description = str(payload.get("description") or "").strip()[:240]
    image_url = str(payload.get("image_url") or "").strip()[:500]
    enabled = int(bool(payload.get("enabled", True)))
    sort_order = _int(payload.get("sort_order", 0), "category_sort_order", maximum=100_000)
    duplicate = conn.execute(
        "SELECT id FROM store_categories WHERE name = ? AND id != COALESCE(?, 0)",
        (name, category_id),
    ).fetchone()
    if duplicate:
        raise StoreError("category_name_already_exists", 409)
    if category_id:
        updated = conn.execute(
            "UPDATE store_categories SET name = ?, description = ?, image_url = ?, enabled = ?, sort_order = ? WHERE id = ?",
            (name, description, image_url, enabled, sort_order, category_id),
        )
        if updated.rowcount != 1:
            raise StoreError("category_not_found", 404)
    else:
        category_id = conn.execute(
            "INSERT INTO store_categories(name, description, image_url, enabled, sort_order) VALUES (?, ?, ?, ?, ?)",
            (name, description, image_url, enabled, sort_order),
        ).lastrowid
    return category_payload(conn.execute("SELECT * FROM store_categories WHERE id = ?", (category_id,)).fetchone())


def save_plan(conn, payload, plan_id=None):
    category_id = _int(payload.get("category_id"), "category_id", minimum=1)
    if not conn.execute("SELECT id FROM store_categories WHERE id = ?", (category_id,)).fetchone():
        raise StoreError("category_not_found", 404)
    name = _name(payload.get("name"), "plan_name", 80)
    product_type = str(payload.get("product_type") or "hosting").strip().lower()
    if product_type not in {"hosting", "minecraft", "discord_bot", "telegram_bot", "minecraft_afk_bot"}:
        raise StoreError("invalid_product_type")
    description = str(payload.get("description") or "").strip()[:1200]
    image_url = str(payload.get("image_url") or "").strip()[:500]
    price_cents = _int(payload.get("price_cents"), "price_cents")
    currency = str(payload.get("currency") or "USD").strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise StoreError("invalid_currency")
    if currency != "USD":
        raise StoreError("only_usd_is_supported")
    billing_interval = str(payload.get("billing_interval") or "month").strip().lower()
    if billing_interval not in {"once", "month"}:
        raise StoreError("invalid_billing_interval")
    limits = payload.get("limits") if isinstance(payload.get("limits"), dict) else {}
    features = payload.get("features") if isinstance(payload.get("features"), dict) else {}
    hosting_plan_id = None
    if product_type == "hosting":
        hosting_plan_id = _int(payload.get("hosting_plan_id"), "hosting_plan_id", minimum=1)
        if not conn.execute("SELECT id FROM plans WHERE id = ?", (hosting_plan_id,)).fetchone():
            raise StoreError("hosting_plan_not_found", 404)
        limits = {}
    elif product_type == "minecraft":
        limits = {
            "ram_mb": _int(limits.get("ram_mb"), "ram_mb", minimum=512, maximum=262144),
            "disk_mb": _int(limits.get("disk_mb", 10240), "disk_mb", minimum=1024, maximum=2_097_152),
            "max_servers": _int(limits.get("max_servers", 1), "max_servers", minimum=1, maximum=100),
        }
        server_types = payload.get("server_types", ["PAPER"])
        if not isinstance(server_types, list) or not server_types or any(
            not re.fullmatch(r"[A-Z][A-Z0-9_]{0,31}", str(item)) for item in server_types
        ):
            raise StoreError("invalid_server_types")
        features = {**features, "server_types": list(dict.fromkeys(server_types))}
    elif product_type in {"discord_bot", "telegram_bot", "minecraft_afk_bot"}:
        limits = {
            "ram_mb": _int(limits.get("ram_mb"), "ram_mb", minimum=512, maximum=262144),
            "disk_mb": _int(limits.get("disk_mb", 10240), "disk_mb", minimum=1024, maximum=2_097_152),
            "cpu_percent": _int(limits.get("cpu_percent", 100), "cpu_percent", minimum=50, maximum=400),
            "ports": _int(limits.get("ports", 1), "ports", minimum=0, maximum=10),
            "domains": _int(limits.get("domains", 1), "domains", minimum=0, maximum=10),
            "databases": _int(limits.get("databases", 1), "databases", minimum=0, maximum=10),
        }
        # Add service-specific features
        if product_type == "discord_bot":
            runtimes = payload.get("runtimes", ["python"])
            if not isinstance(runtimes, list) or not runtimes or any(
                not re.fullmatch(r"[a-z]+", str(item).lower()) for item in runtimes
            ):
                raise StoreError("invalid_runtimes")
            features = {**features, "runtimes": [r.lower() for r in runtimes]}
        elif product_type == "telegram_bot":
            runtimes = payload.get("runtimes", ["python"])
            if not isinstance(runtimes, list) or not runtimes or any(
                not re.fullmatch(r"[a-z]+", str(item).lower()) for item in runtimes
            ):
                raise StoreError("invalid_runtimes")
            features = {**features, "runtimes": [r.lower() for r in runtimes]}
        elif product_type == "minecraft_afk_bot":
            limits["max_bots"] = _int(limits.get("max_bots", 1), "max_bots", minimum=1, maximum=10)
            limits["runtime_limit_hours"] = _int(limits.get("runtime_limit_hours", 24), "runtime_limit_hours", minimum=1, maximum=720)
    else:
        limits = {}
    enabled = int(bool(payload.get("enabled", True)))
    duplicate = conn.execute(
        "SELECT id FROM store_plans WHERE category_id = ? AND name = ? AND id != COALESCE(?, 0)",
        (category_id, name, plan_id),
    ).fetchone()
    if duplicate:
        raise StoreError("plan_name_already_exists", 409)
    values = (category_id, name, description, image_url, product_type, hosting_plan_id, price_cents, currency, billing_interval,
              json.dumps(limits, sort_keys=True), json.dumps(features, sort_keys=True), enabled)
    if plan_id:
        updated = conn.execute(
            """UPDATE store_plans SET category_id=?, name=?, description=?, image_url=?, product_type=?, hosting_plan_id=?,
               price_cents=?, currency=?, billing_interval=?, limits_json=?, features_json=?, enabled=? WHERE id=?""",
            (*values, plan_id),
        )
        if updated.rowcount != 1:
            raise StoreError("store_plan_not_found", 404)
    else:
        plan_id = conn.execute(
            """INSERT INTO store_plans(category_id, name, description, image_url, product_type, hosting_plan_id, price_cents,
               currency, billing_interval, limits_json, features_json, enabled) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            values,
        ).lastrowid
    return plan_payload(conn.execute("SELECT * FROM store_plans WHERE id = ?", (plan_id,)).fetchone())


def place_order(conn, user_id, store_plan_id):
    plan = conn.execute(
        """SELECT sp.*, sc.enabled AS category_enabled FROM store_plans sp
           JOIN store_categories sc ON sc.id = sp.category_id WHERE sp.id = ? AND sp.enabled = 1""",
        (store_plan_id,),
    ).fetchone()
    if not plan or not plan["category_enabled"]:
        raise StoreError("store_plan_not_available", 404)
    if plan["product_type"] == "hosting" and not conn.execute(
        "SELECT id FROM plans WHERE id = ?", (plan["hosting_plan_id"],)
    ).fetchone():
        raise StoreError("hosting_plan_not_found", 409)
    wallet(conn, user_id)
    snapshot = plan_payload(plan)
    order_id = conn.execute(
        """INSERT INTO store_orders(user_id, store_plan_id, amount_cents, currency, product_type, plan_snapshot_json)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (user_id, store_plan_id, plan["price_cents"], plan["currency"], plan["product_type"],
         json.dumps(snapshot, sort_keys=True)),
    ).lastrowid
    return order(conn, order_id, user_id=user_id)


def order(conn, order_id, user_id=None):
    query = """SELECT o.*, u.email, u.full_name, sp.name AS plan_name, sc.name AS category_name
               FROM store_orders o JOIN users u ON u.id = o.user_id
               JOIN store_plans sp ON sp.id = o.store_plan_id
               JOIN store_categories sc ON sc.id = sp.category_id WHERE o.id = ?"""
    params = [order_id]
    if user_id is not None:
        query += " AND o.user_id = ?"
        params.append(user_id)
    row = conn.execute(query, params).fetchone()
    if not row:
        raise StoreError("order_not_found", 404)
    result = row_to_dict(row)
    result["plan_snapshot"] = _json(result.pop("plan_snapshot_json", "{}"), {})
    return result


def approve_order(conn, order_id, admin_id, account_root):
    order_row = conn.execute("SELECT * FROM store_orders WHERE id = ?", (order_id,)).fetchone()
    if not order_row:
        raise StoreError("order_not_found", 404)
    if order_row["status"] != "pending":
        raise StoreError("order_not_pending", 409)
    balance = wallet(conn, order_row["user_id"])
    amount = int(order_row["amount_cents"])
    if balance < amount:
        raise StoreError("insufficient_balance", 409)
    if amount:
        updated = conn.execute(
            "UPDATE wallets SET balance_cents = balance_cents - ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND balance_cents >= ?",
            (amount, order_row["user_id"], amount),
        )
        if updated.rowcount != 1:
            raise StoreError("insufficient_balance", 409)
        conn.execute(
            "INSERT INTO wallet_transactions(user_id, order_id, delta_cents, reference, description) VALUES (?, ?, ?, ?, ?)",
            (order_row["user_id"], order_id, -amount, f"order:{order_id}", f"Approved order #{order_id}"),
        )
    snapshot = _json(order_row["plan_snapshot_json"], {})
    service_id = None
    if order_row["product_type"] == "hosting":
        plan_id = int(snapshot.get("hosting_plan_id") or 0)
        if not conn.execute("SELECT id FROM plans WHERE id = ?", (plan_id,)).fetchone():
            raise StoreError("hosting_plan_not_found", 409)
        node = conn.execute("SELECT id FROM nodes WHERE status = 'online' ORDER BY id LIMIT 1").fetchone()
        if not node:
            raise StoreError("no_active_node", 409)
        account_count = conn.execute("SELECT COUNT(*) AS count FROM hosting_accounts WHERE user_id = ?", (order_row["user_id"],)).fetchone()["count"]
        username = f"u{int(order_row['user_id']):06d}x{int(account_count) + 1}"
        while conn.execute("SELECT id FROM hosting_accounts WHERE username = ?", (username,)).fetchone():
            username += "x"
        base_path = str(account_root / username)
        service_id = conn.execute(
            """INSERT INTO hosting_accounts(user_id, plan_id, node_id, username, base_path, status)
               VALUES (?, ?, ?, ?, ?, 'provisioning')""",
            (order_row["user_id"], plan_id, node["id"], username, base_path),
        ).lastrowid
        create_job(conn, "provision_hosting_account", "hosting_account", service_id, {"store_order_id": order_id})
    elif order_row["product_type"] == "minecraft":
        server_name = f"Minecraft #{order_id}"
        limits = snapshot.get("limits") if isinstance(snapshot.get("limits"), dict) else {}
        max_servers = int(limits.get("max_servers", 1))
        used_servers = conn.execute(
            "SELECT COUNT(*) AS count FROM minecraft_servers WHERE user_id = ? AND store_plan_id = ?",
            (order_row["user_id"], order_row["store_plan_id"]),
        ).fetchone()["count"]
        if used_servers >= max_servers:
            raise StoreError("minecraft_server_limit_reached", 409)
        account_root = account_root.parent / "minecraft"
        root_path = str(account_root / f"u{int(order_row['user_id']):06d}" / f"server-{order_id}")
        server_types = snapshot.get("features", {}).get("server_types", ["PAPER"])
        service_id = conn.execute(
            """INSERT INTO minecraft_servers(user_id, store_order_id, store_plan_id, name, server_type, version,
               ram_mb, disk_mb, container_name, root_path, status) VALUES (?, ?, ?, ?, ?, 'latest', ?, ?, ?, ?, 'stopped')""",
            (order_row["user_id"], order_id, order_row["store_plan_id"], server_name,
             server_types[0] if server_types else "PAPER", int(limits.get("ram_mb", 1024)),
             int(limits.get("disk_mb", 10240)), f"zeropanel-minecraft-{order_id}", root_path),
        ).lastrowid
    elif order_row["product_type"] in {"discord_bot", "telegram_bot", "minecraft_afk_bot"}:
        service_names = {
            "discord_bot": f"Discord Bot #{order_id}",
            "telegram_bot": f"Telegram Bot #{order_id}",
            "minecraft_afk_bot": f"Minecraft AFK Bot #{order_id}",
        }
        service_name = service_names.get(order_row["product_type"], f"Application Server #{order_id}")
        limits = snapshot.get("limits") if isinstance(snapshot.get("limits"), dict) else {}
        features = snapshot.get("features") if isinstance(snapshot.get("features"), dict) else {}
        
        # Set default runtime based on service type
        default_runtime = "python"
        if order_row["product_type"] == "discord_bot":
            runtimes = features.get("runtimes", ["python"])
            default_runtime = runtimes[0] if runtimes else "python"
        elif order_row["product_type"] == "telegram_bot":
            runtimes = features.get("runtimes", ["python"])
            default_runtime = runtimes[0] if runtimes else "python"
        elif order_row["product_type"] == "minecraft_afk_bot":
            default_runtime = "python"  # AFK bot uses Python by default
        
        # Set default commands based on runtime
        default_commands = {
            "python": {"main_file": "main.py", "install_command": "pip install -r requirements.txt", "start_command": "python main.py"},
            "node": {"main_file": "index.js", "install_command": "npm install", "start_command": "node index.js"},
            "typescript": {"main_file": "index.ts", "install_command": "npm install", "build_command": "npm run build", "start_command": "npm start"},
        }
        commands = default_commands.get(default_runtime, default_commands["python"])
        
        account_root = account_root.parent / "application_servers"
        root_path = str(account_root / f"u{int(order_row['user_id']):06d}" / f"service-{order_id}")
        
        service_id = conn.execute(
            """INSERT INTO application_servers(user_id, store_order_id, store_plan_id, service_type, name, runtime, 
               main_file, install_command, start_command, build_command, ram_mb, disk_mb, container_name, root_path, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'stopped')""",
            (order_row["user_id"], order_id, order_row["store_plan_id"], order_row["product_type"], service_name,
             default_runtime, commands["main_file"], commands["install_command"], commands["start_command"],
             commands.get("build_command", ""), int(limits.get("ram_mb", 1024)), int(limits.get("disk_mb", 10240)),
             f"zeropanel-app-{order_id}", root_path),
        ).lastrowid
        create_job(conn, "provision_application_server", "application_server", service_id, {"store_order_id": order_id})
    else:
        raise StoreError("unsupported_product_type", 400)
    conn.execute(
        """UPDATE store_orders SET status = 'approved', service_id = ?, reviewed_by = ?,
           renewal_at = CASE WHEN json_extract(plan_snapshot_json, '$.billing_interval') = 'month' THEN datetime('now', '+1 month') ELSE NULL END,
           updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'pending'""",
        (service_id, admin_id, order_id),
    )
    return order(conn, order_id)


def reject_order(conn, order_id, admin_id, note=""):
    updated = conn.execute(
        "UPDATE store_orders SET status = 'rejected', reviewed_by = ?, review_note = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'pending'",
        (admin_id, str(note or "").strip()[:500], order_id),
    )
    if updated.rowcount != 1:
        if not conn.execute("SELECT id FROM store_orders WHERE id = ?", (order_id,)).fetchone():
            raise StoreError("order_not_found", 404)
        raise StoreError("order_not_pending", 409)
    return order(conn, order_id)


def renew_due_orders(conn):
    due_orders = conn.execute(
        "SELECT * FROM store_orders WHERE status IN ('approved', 'suspended') AND renewal_at <= CURRENT_TIMESTAMP ORDER BY renewal_at, id"
    ).fetchall()
    processed = 0
    for due in due_orders:
        order_id = int(due["id"])
        savepoint = f"store_renewal_{order_id}"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            snapshot = _json(due["plan_snapshot_json"], {})
            amount = int(due["amount_cents"])
            current_balance = wallet(conn, due["user_id"])
            if current_balance >= amount:
                if amount:
                    updated = conn.execute(
                        "UPDATE wallets SET balance_cents = balance_cents - ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND balance_cents >= ?",
                        (amount, due["user_id"], amount),
                    )
                    if updated.rowcount != 1:
                        raise StoreError("insufficient_balance", 409)
                    conn.execute(
                        "INSERT INTO wallet_transactions(user_id, order_id, delta_cents, reference, description) VALUES (?, ?, ?, ?, ?)",
                        (due["user_id"], order_id, -amount, f"renewal:{order_id}:{due['renewal_at']}", f"Monthly renewal for order #{order_id}"),
                    )
                if due["billing_suspended"]:
                    if due["product_type"] == "hosting":
                        conn.execute(
                            "UPDATE hosting_accounts SET status = 'active' WHERE id = ? AND status = 'suspended'",
                            (due["service_id"],),
                        )
                    elif due["product_type"] == "minecraft":
                        server = conn.execute("SELECT * FROM minecraft_servers WHERE id = ?", (due["service_id"],)).fetchone()
                        if server:
                            status = "stopped"
                            port = server["port"]
                            if due["was_running_before_billing"]:
                                try:
                                    from .minecraft import server_action
                                    result = server_action(row_to_dict(server), "start")
                                    status, port = result["status"], result.get("port")
                                except Exception:
                                    pass
                            conn.execute("UPDATE minecraft_servers SET status = ?, port = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (status, port, server["id"]))
                    elif due["product_type"] in {"discord_bot", "telegram_bot", "minecraft_afk_bot"}:
                        conn.execute(
                            "UPDATE application_servers SET status = 'stopped' WHERE id = ? AND status = 'suspended'",
                            (due["service_id"],),
                        )
                conn.execute(
                    """UPDATE store_orders SET status = 'approved', billing_suspended = 0,
                       was_running_before_billing = 0, renewal_at = datetime('now', '+1 month'),
                       updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                    (order_id,),
                )
            else:
                was_running = 0
                if due["product_type"] == "hosting":
                    conn.execute(
                        "UPDATE hosting_accounts SET status = 'suspended' WHERE id = ? AND status IN ('active', 'provisioning')",
                        (due["service_id"],),
                    )
                elif due["product_type"] == "minecraft":
                    server = conn.execute("SELECT * FROM minecraft_servers WHERE id = ?", (due["service_id"],)).fetchone()
                    if server:
                        was_running = int(due["was_running_before_billing"] or server["status"] == "running")
                        if server["status"] == "running":
                            try:
                                from .minecraft import server_action
                                server_action(row_to_dict(server), "stop")
                            except Exception:
                                pass
                        conn.execute("UPDATE minecraft_servers SET status = 'suspended', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (server["id"],))
                elif due["product_type"] in {"discord_bot", "telegram_bot", "minecraft_afk_bot"}:
                    app_server = conn.execute("SELECT * FROM application_servers WHERE id = ?", (due["service_id"],)).fetchone()
                    if app_server:
                        was_running = int(due["was_running_before_billing"] or app_server["status"] == "running")
                        if app_server["status"] == "running":
                            conn.execute("UPDATE application_servers SET status = 'stopped', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (app_server["id"],))
                        conn.execute("UPDATE application_servers SET status = 'suspended', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (app_server["id"],))
                conn.execute(
                    """UPDATE store_orders SET status = 'suspended', billing_suspended = 1,
                       was_running_before_billing = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
                    (was_running, order_id),
                )
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            processed += 1
        except Exception:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
    return processed