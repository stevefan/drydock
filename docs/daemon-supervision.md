# Supervising `wsd`

Use `ws daemon start --foreground` when a real service manager is in charge. In that mode, `ws` does not detach; `launchd` or `systemd` owns restart policy, stdout/stderr capture, and lifecycle.

If you are not using a supervisor, run `ws daemon start` manually or from a login script. That path uses Drydock's built-in detach logic and writes to `~/.drydock/wsd.pid` and `~/.drydock/wsd.log`.

## macOS launchd

Write `~/Library/LaunchAgents/com.drydock.wsd.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>com.drydock.wsd</string>

    <key>ProgramArguments</key>
    <array>
      <string>/bin/zsh</string>
      <string>-lc</string>
      <string>ws daemon start --foreground</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <dict>
      <key>SuccessfulExit</key>
      <false/>
    </dict>

    <key>StandardOutPath</key>
    <string>/Users/YOUR_USER/.drydock/wsd.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/YOUR_USER/.drydock/wsd.log</string>

    <key>EnvironmentVariables</key>
    <dict>
      <key>PATH</key>
      <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
  </dict>
</plist>
```

Quick start:

```bash
launchctl unload ~/Library/LaunchAgents/com.drydock.wsd.plist 2>/dev/null || true
launchctl load ~/Library/LaunchAgents/com.drydock.wsd.plist
launchctl start com.drydock.wsd
ws daemon status
```

## Linux systemd user unit

Write `~/.config/systemd/user/wsd.service`:

```ini
[Unit]
Description=Drydock Harbor daemon
After=default.target

[Service]
Type=simple
ExecStart=/bin/sh -lc 'ws daemon start --foreground'
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
```

Quick start:

```bash
systemctl --user daemon-reload
systemctl --user enable --now wsd.service
systemctl --user status wsd.service
ws daemon status
```
