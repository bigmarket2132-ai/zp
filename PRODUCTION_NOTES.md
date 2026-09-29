# ZeroPanel Production Notes

## Install

1. Clone the repository onto the server.
2. Run the installer:

```bash
bash scripts/install.sh --full
```

3. Start ZeroPanel:

```bash
bash scripts/service Zeropanel start
```

The installer now registers a boot-time service:
- Linux: `systemd`
- macOS: `launchd` LaunchAgent

That means ZeroPanel is configured to start again after a reboot.

## Start

```bash
bash scripts/service Zeropanel start
```

If you prefer to use the service manager directly:
- Linux: `systemctl start Zeropanel`
- macOS: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.servermango.Zeropanel.plist`

## Stop

```bash
bash scripts/service Zeropanel stop
```

If you prefer to use the service manager directly:
- Linux: `systemctl stop Zeropanel`
- macOS: `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.servermango.Zeropanel.plist`

## Status

```bash
bash scripts/service Zeropanel status
```

## Logs

- Service log: `var/Zeropanel.log`
- PID file: `var/Zeropanel.pid`

