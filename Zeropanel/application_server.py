import json
import os
import subprocess
from pathlib import Path
from .db import row_to_dict, rows_to_dicts


class ApplicationServerError(Exception):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code = code
        self.status = status


def get_application_servers(conn, user_id):
    """Get all application servers for a user."""
    servers = conn.execute(
        "SELECT * FROM application_servers WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    ).fetchall()
    return rows_to_dicts(servers)


def get_application_server(conn, server_id, user_id=None):
    """Get a specific application server."""
    query = "SELECT * FROM application_servers WHERE id = ?"
    params = [server_id]
    if user_id is not None:
        query += " AND user_id = ?"
        params.append(user_id)
    
    server = conn.execute(query, params).fetchone()
    if not server:
        raise ApplicationServerError("application_server_not_found", 404)
    
    result = row_to_dict(server)
    result["environment"] = json.loads(result.get("environment_json", "{}"))
    result["config"] = json.loads(result.get("config_json", "{}"))
    return result


def create_application_server(conn, user_id, store_order_id, store_plan_id, service_type, name, 
                             runtime, ram_mb, disk_mb, root_path):
    """Create a new application server."""
    server_id = conn.execute(
        """INSERT INTO application_servers(user_id, store_order_id, store_plan_id, service_type, name, 
           runtime, ram_mb, disk_mb, root_path, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'stopped')""",
        (user_id, store_order_id, store_plan_id, service_type, name, runtime, ram_mb, disk_mb, root_path),
    ).lastrowid
    return get_application_server(conn, server_id)


def update_application_server(conn, server_id, user_id, updates):
    """Update application server configuration."""
    allowed_fields = {
        "name", "runtime", "main_file", "install_command", "start_command", "build_command",
        "environment", "config"
    }
    
    # Filter updates to only allowed fields
    valid_updates = {k: v for k, v in updates.items() if k in allowed_fields}
    
    if not valid_updates:
        raise ApplicationServerError("no_valid_updates")
    
    # Handle JSON fields
    json_fields = {"environment", "config"}
    set_clauses = []
    params = []
    
    for field, value in valid_updates.items():
        if field in json_fields:
            set_clauses.append(f"{field}_json = ?")
            params.append(json.dumps(value, sort_keys=True))
        else:
            set_clauses.append(f"{field} = ?")
            params.append(value)
    
    params.extend([server_id, user_id])
    
    updated = conn.execute(
        f"UPDATE application_servers SET {', '.join(set_clauses)}, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",
        params,
    )
    
    if updated.rowcount != 1:
        raise ApplicationServerError("application_server_not_found", 404)
    
    return get_application_server(conn, server_id, user_id)


def server_action(conn, server_id, user_id, action):
    """Perform server actions (start, stop, restart)."""
    server = get_application_server(conn, server_id, user_id)
    
    if action not in {"start", "stop", "restart"}:
        raise ApplicationServerError("invalid_action")
    
    # In a real implementation, this would interact with Docker/containers
    # For now, we'll update the status in the database
    if action == "start":
        new_status = "running"
    elif action == "stop":
        new_status = "stopped"
    else:  # restart
        new_status = "running"
    
    conn.execute(
        "UPDATE application_servers SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (new_status, server_id),
    )
    
    return {"status": new_status, "server_id": server_id}


def get_server_logs(conn, server_id, user_id, lines=100):
    """Get server logs."""
    server = get_application_server(conn, server_id, user_id)
    
    # In a real implementation, this would read from container logs
    # For now, return placeholder
    return {
        "server_id": server_id,
        "logs": [
            {"timestamp": "2024-01-01 12:00:00", "level": "info", "message": "Server started"},
            {"timestamp": "2024-01-01 12:00:01", "level": "info", "message": "Application loaded"},
        ],
    }


def get_server_stats(conn, server_id, user_id):
    """Get server resource usage statistics."""
    server = get_application_server(conn, server_id, user_id)
    
    # In a real implementation, this would get actual Docker stats
    # For now, return placeholder data
    return {
        "server_id": server_id,
        "status": server["status"],
        "cpu_percent": 24.5,
        "memory_mb": 420,
        "memory_limit_mb": server["ram_mb"],
        "disk_mb": 1200,
        "disk_limit_mb": server["disk_mb"],
        "network_down_kbps": 200,
        "network_up_kbps": 80,
        "uptime_seconds": 180000,  # ~2 days
    }


def delete_application_server(conn, server_id, user_id):
    """Delete an application server."""
    deleted = conn.execute(
        "DELETE FROM application_servers WHERE id = ? AND user_id = ?",
        (server_id, user_id),
    )
    
    if deleted.rowcount != 1:
        raise ApplicationServerError("application_server_not_found", 404)
    
    return True
