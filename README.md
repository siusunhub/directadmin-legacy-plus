# DirectAdmin Legacy Plus

[![GitHub Repo](https://img.shields.io/badge/GitHub-siusun%2Fdirectadmin--legacy--plus-181717?logo=github&logoColor=white)](https://github.com/siusun/directadmin-legacy-plus)
[![GitHub stars](https://img.shields.io/github/stars/siusun/directadmin-legacy-plus?style=social)](https://github.com/siusun/directadmin-legacy-plus)
[![GitHub forks](https://img.shields.io/github/forks/siusun/directadmin-legacy-plus?style=social)](https://github.com/siusun/directadmin-legacy-plus)
[![Version](https://img.shields.io/badge/version-v0.1.0-blue.svg)](https://github.com/siusun/directadmin-legacy-plus)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![cgroups](https://img.shields.io/badge/cgroups-v2-informational.svg)](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html)
[![DirectAdmin](https://img.shields.io/badge/DirectAdmin-Legacy-orange.svg)](https://www.directadmin.com/)

**DirectAdmin Legacy Plus (`da-plus`)** is a lightweight, non-invasive resource governor and live monitoring suite for legacy DirectAdmin web hosting servers, built natively on **Linux systemd cgroups v2**.

It enforces memory, CPU, block I/O, and process limits directly at the Linux user slice level (`user-<uid>.slice`), features an interactive terminal UI (TUI) dashboard with live sparkline metrics, and provisions secure, isolated per-account Redis instances—**without modifying DirectAdmin binaries, core files, or licensing**.

---

## Key Features

- **Native cgroups v2 Resource Governance**
  - Hard CPU quotas (`CPUQuota`) and soft/hard two-stage memory throttling (`MemoryHigh`, `MemoryMax`).
  - Disk read/write bandwidth caps and IOPS rate limiting.
  - Process/thread ceiling (`TasksMax`) to prevent fork bombs and runaway background jobs.
  - Persistently toggle account limits `on`/`off` without losing hosting profiles or configurations.
  - Built-in exemption for the system `admin` account from CPU/IO throttling.
- **DirectAdmin Integration**
  - Automatically discovers accounts and packages via DirectAdmin's native configs.
  - Non-blocking event hooks (`user_create_post`, `user_modify_post`, `user_destroy_post`).
  - 15-minute background reconciliation timer (`da-plus-reconcile.timer`) to guarantee state consistency.
- **Isolated Per-Account Redis**
  - Dedicated unprivileged Redis instances running inside each user's own cgroup slice.
  - Zero exposed network ports; communicates strictly over protected Unix domain sockets (`0700`).
  - Automated 256-bit authentication passwords stored in user home directories (`0600`).
- **Interactive Terminal UI (TUI)**
  - Full-screen dashboard built on Textual with real-time sparkline usage graphs.
  - Visual limits and Redis profile editors with built-in diff previews before saving.
  - Masked Redis passwords with a 60-second timed reveal button and auto-re-masking.
  - Plain monochrome mode (`--plain`) for basic serial/remote consoles.
- **Safety First**
  - Atomic configuration drop-ins in `/etc/systemd/system/user-<uid>.slice.d/50-da-plus.conf`.
  - Comprehensive `--dry-run` modes and interactive preflight checks with clear problem guidance.
  - Automated backups, orphan cleanup utilities, and clean uninstallation.

---

## Requirements

- **OS:** Linux with unified **systemd cgroups v2** (verify with `stat -fc %T /sys/fs/cgroup` &rarr; `cgroup2fs`).
- **Control Panel:** DirectAdmin (Legacy installations).
- **Python:** Python `>= 3.10` with `pyyaml>=6` and `textual>=8`.
- **Privileges:** Root access (`sudo`) for applying systemd drop-ins.

---

## Installation & Setup

### Option A: Standard Package Install (Recommended)

Install using a dedicated virtual environment to avoid conflicts with system packages:
```bash
# 1. Create a dedicated virtual environment
sudo python3 -m venv /opt/da-plus/venv

# 2. Install dependencies & da-plus
sudo /opt/da-plus/venv/bin/pip install "pyyaml>=6" "textual>=8"
sudo /opt/da-plus/venv/bin/pip install .

# 3. Symlink da-plus globally into your PATH
sudo ln -sf /opt/da-plus/venv/bin/da-plus /usr/local/bin/da-plus
```

### Option B: Direct Module Run (No Build Tools)

Install dependencies and run directly from the source directory:
```bash
sudo python3 -m pip install "pyyaml>=6" "textual>=8"
# Run via python3 -m da_plus (e.g. from the da-plus/ directory)
```

---

## Initial Configuration & Deployment

### 1. Initialize Configuration
```bash
sudo mkdir -p /etc/da-plus
sudo cp config/example.yml /etc/da-plus/config.yml
sudo chmod 0640 /etc/da-plus/config.yml
```
Validate syntax:
```bash
da-plus validate
```

### 2. Run the Interactive Installer
```bash
sudo da-plus install
```
The installer guides you through safe deployment:
- **`0. Test install`**: Pure dry-run that checks configuration, unit paths, controllers, and services without writing to disk (`da-plus install --dry-run`).
- **`1. Real install`**: Confirms paths, deploys systemd delegation, DirectAdmin hooks, and activates the reconcile timer.
- **`2. Force remove Redis`**: Safe cleanup workflow for obsolete shared Redis instances.
- **`9. Quit`**: Exit with zero changes.

For unattended / scripted installation:
```bash
sudo da-plus install --yes --hooks --enable-timer
```

---

## Quick Usage

### Interactive Terminal Dashboard (TUI)
Launch the real-time TUI dashboard:
```bash
# Root mode (manage limits, toggle Redis, live telemetry)
sudo da-plus tui

# Read-only inspection mode
da-plus tui

# Plain monochrome mode for serial / basic terminals
da-plus tui --plain
```

### Common CLI Commands

```bash
# 1. List DirectAdmin users, assigned packages, and resolved profiles
da-plus users

# 2. Inspect live cgroup resource consumption (memory, CPU, IO, tasks, OOM)
da-plus usage <username>

# 3. Preview rendered systemd drop-in configuration
da-plus show <username>

# 4. Apply resource limits
sudo da-plus apply <username>      # Single user
sudo da-plus apply --all           # All users across the server
da-plus apply --all --dry-run      # Preview without applying

# 5. Persistently enable / disable account limits
sudo da-plus limits <username> off --yes   # Turn off limits for user
sudo da-plus limits <username> on --yes    # Re-enable limits

# 6. Manage per-account Redis instances
da-plus redis-user status <username>
sudo da-plus redis-user enable <username> --yes
sudo da-plus redis-user disable <username> --yes
sudo da-plus redis-user rotate-password <username> --yes

# 7. Shared Redis management & cleanup
da-plus redis status
sudo da-plus redis-cleanup --yes
```

---

## CLI Reference

| Command | Arguments / Flags | Description |
|---|---|---|
| `tui` | `[--plain]` | Launches the interactive terminal dashboard. |
| `users` | _None_ | Lists discovered DirectAdmin accounts, packages, and profiles. |
| `usage` | `<user>` | Displays live cgroup v2 resource telemetry. |
| `show` | `<user>` | Displays effective limits and rendered systemd drop-in directives. |
| `apply` | `[user] [--all] [--dry-run]` | Applies drop-ins and reloads systemd properties on the fly. |
| `limits` | `<user> <on\|off> [--yes]` | Persistently enables or disables limits for an account. |
| `validate` | _None_ | Validates `/etc/da-plus/config.yml` syntax and rules. |
| `install` | `[--hooks] [--enable-timer] [--dry-run] [--yes]` | Runs the interactive installer or automated headless setup. |
| `uninstall` | `[--yes] [--reset-limits] [--purge-config] [--purge-logs]` | Cleanly removes drop-ins, hooks, and timer units. |
| `redis` | `status \| install [--yes] [--dry-run]` | Inspects or installs the shared system Redis server. |
| `redis-user` | `status\|enable\|disable\|rotate-password <user> [--yes]` | Manages isolated per-user Unix socket Redis services. |
| `redis-cleanup` | `[--yes] [--kill-orphan-process]` | Detects and safely disables orphan Redis systemd units. |
| `redis-force-remove`| `[--purge-data]` | Cleanly purges legacy shared Redis with multi-stage backups. |

---

## Configuration Example (`config.yml`)

The configuration file resides at `/etc/da-plus/config.yml`:

```yaml
version: 1

defaults:
  profile: basic
  io_device: /dev/sda          # Storage block device backing /home

profiles:
  basic:
    cpu_quota: 50%              # 50% of 1 CPU core
    memory_high: 512M           # Soft memory throttle threshold
    memory_max: 768M            # Hard cap (triggers cgroup OOM)
    io_read_bw: 50M             # Read bandwidth limit (bytes/sec)
    io_write_bw: 20M            # Write bandwidth limit
    iops_read: 1000             # Max read IOPS
    iops_write: 500             # Max write IOPS
    tasks_max: 200              # Max processes / threads
    redis:
      enabled: false            # Redis allowed for profile
      maxmemory: 64M            # Redis memory allocation
      maxmemory_policy: allkeys-lru
      persistence: off          # Persistence mode: off or rdb

  business:
    cpu_quota: 150%             # 1.5 CPU cores
    memory_high: 1G
    memory_max: 1536M
    io_read_bw: 100M
    io_write_bw: 50M
    iops_read: 3000
    iops_write: 1500
    tasks_max: 500
    redis:
      enabled: false
      maxmemory: 256M
      maxmemory_policy: allkeys-lru
      persistence: off

# DirectAdmin package name -> profile mapping
package_map:
  Starter: basic
  Business: business

# Per-user overrides
users:
  fred:
    profile: business
    overrides:
      memory_max: 2G
```

---

## Uninstallation

To cleanly remove `da-plus` from the server:

```bash
# Preview uninstallation changes (dry-run)
sudo da-plus uninstall

# Execute removal and reset runtime slice limits to default
sudo da-plus uninstall --yes --reset-limits

# Full purge including configuration and logs
sudo da-plus uninstall --yes --reset-limits --purge-config --purge-logs
```

---

## License

This project is licensed under the [MIT License](LICENSE).
