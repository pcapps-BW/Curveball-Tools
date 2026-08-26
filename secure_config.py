#!/usr/bin/env python3
"""
secure_config.py -- Credential manager for the Curveball tool suite.

Passwords are stored in Windows Credential Manager via the keyring library.
Non-sensitive config (usernames, emails, URLs) stays in autotester_config.txt.

Sensitive keys managed here:
    INFRA_PASS    -- 1250 / 1800 / MUX password
    GSX_PASS      -- GSX password
    JIRA_TOKEN    -- Atlassian API token

Non-sensitive keys stay in autotester_config.txt as before:
    INFRA_USER, GSX_USER, JIRA_EMAIL, JIRA_ASSIGNEE_EMAIL, SLACK_WEBHOOK

Migration: if a sensitive key is found in plaintext in the config file on first
load, it is migrated to Credential Manager and the plaintext line is removed.

Usage (other modules):
    from secure_config import load_config, get_password, set_password

Built by P. Capps.
"""

import os
import getpass

try:
    import keyring
except ImportError:
    keyring = None

CONFIG_PATH   = r"C:\Claude\autotester_config.txt"
KEYRING_APP   = "curveball"

# Keys stored in Credential Manager (never written to disk as plaintext)
SECURE_KEYS = ("INFRA_PASS", "GSX_PASS", "JIRA_TOKEN")

# Keys that stay in the config file (non-sensitive)
CONFIG_KEYS = ("INFRA_USER", "GSX_USER", "JIRA_EMAIL",
               "JIRA_ASSIGNEE_EMAIL", "SLACK_WEBHOOK")

# Human-readable prompts for first-run setup
SECURE_PROMPTS = {
    "INFRA_PASS":  "Infrastructure password (1250 / 1800 / MUX)",
    "GSX_PASS":    "GSX password",
    "JIRA_TOKEN":  "JIRA API token",
}


# ── Keyring wrappers ──────────────────────────────────────────────────────────

def _keyring_available():
    return keyring is not None


def get_password(key):
    """Retrieve a password from Credential Manager. Returns None if not set."""
    if not _keyring_available():
        return None
    try:
        return keyring.get_password(KEYRING_APP, key)
    except Exception as e:
        print("  [keyring] get '{}' failed: {}".format(key, e))
        return None


def set_password(key, value):
    """Store a password in Credential Manager."""
    if not _keyring_available():
        print("  [keyring] not available -- password not stored securely.")
        return False
    try:
        keyring.set_password(KEYRING_APP, key, value)
        return True
    except Exception as e:
        print("  [keyring] set '{}' failed: {}".format(key, e))
        return False


def delete_password(key):
    """Remove a password from Credential Manager."""
    if not _keyring_available():
        return
    try:
        keyring.delete_password(KEYRING_APP, key)
    except Exception:
        pass


# ── Config file helpers ───────────────────────────────────────────────────────

def _read_config_file(path=CONFIG_PATH):
    """Read all key=value pairs from config file. Returns dict."""
    cfg = {}
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                cfg[key.strip()] = val.strip()
    except FileNotFoundError:
        pass
    return cfg


def _write_config_file(cfg, path=CONFIG_PATH):
    """Write non-sensitive config keys to file, preserving comments."""
    # Read existing lines to preserve comments/formatting
    existing_lines = []
    try:
        with open(path, "r") as f:
            existing_lines = f.readlines()
    except FileNotFoundError:
        pass

    # Remove any lines for keys we're managing (will re-add)
    managed = set(list(CONFIG_KEYS) + list(SECURE_KEYS))
    kept = []
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            kept.append(line)
            continue
        if "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key not in managed:
                kept.append(line)
            # Drop ALL managed keys -- we'll re-add non-sensitive ones below
            # Secure keys are intentionally NOT written back
        else:
            kept.append(line)

    # Append current non-sensitive values
    if kept and kept[-1].strip():
        kept.append("\n")
    for key in CONFIG_KEYS:
        if key in cfg and cfg[key]:
            kept.append("{}={}\n".format(key, cfg[key]))

    try:
        with open(path, "w") as f:
            f.writelines(kept)
    except Exception as e:
        print("  [config] write failed: {}".format(e))


def _migrate_plaintext_passwords(cfg, path=CONFIG_PATH):
    """
    If any SECURE_KEYS are found in plaintext in the config file,
    migrate them to Credential Manager and remove from the file.
    Returns the updated cfg dict (with secure keys removed).
    """
    migrated = []
    for key in SECURE_KEYS:
        if key in cfg and cfg[key]:
            val = cfg.pop(key)
            if set_password(key, val):
                migrated.append(key)
                print("  [secure_config] Migrated '{}' to Credential Manager -- "
                      "removed from config file.".format(key))
            else:
                # Couldn't store -- put it back so caller still gets it
                cfg[key] = val

    if migrated:
        # Rewrite file without the migrated plaintext lines
        _write_config_file(cfg, path)

    return cfg


# ── Main interface ────────────────────────────────────────────────────────────

def load_config(path=CONFIG_PATH):
    """
    Load full configuration. Returns a dict with ALL keys populated:
      - Non-sensitive: from autotester_config.txt
      - Sensitive:     from Credential Manager (keyring)

    On first run (missing secure keys): prompts the user, stores in
    Credential Manager, never writes passwords to disk.

    Migrates any plaintext passwords found in the config file automatically.
    """
    # Read file-based config
    cfg = _read_config_file(path)

    # Migrate any plaintext passwords that crept in
    cfg = _migrate_plaintext_passwords(cfg, path)

    # Load secure keys from Credential Manager
    missing_secure = []
    for key in SECURE_KEYS:
        val = get_password(key)
        if val:
            cfg[key] = val
        else:
            missing_secure.append(key)

    # First-run setup: prompt for any missing secure keys
    if missing_secure:
        print("\n  -- First-time credential setup (stored securely in Windows Credential Manager) --")
        for key in missing_secure:
            prompt = SECURE_PROMPTS.get(key, key)
            while True:
                val = getpass.getpass("  {}: ".format(prompt)).strip()
                if val:
                    break
                print("  Required -- cannot be blank.")
            if set_password(key, val):
                print("  '{}' stored in Credential Manager.".format(key))
            cfg[key] = val

    return cfg


def load_config_silent(path=CONFIG_PATH):
    """
    Like load_config(), but NEVER prompts -- for callers that can't block on
    interactive input (e.g. a Flask request handler; getpass() there would
    hang the request/server, not just the terminal). Missing secure keys are
    simply left out of the returned dict instead of triggering a prompt.
    Still performs the plaintext-password migration (a file fix, not a
    prompt) and still reads whatever IS already in Credential Manager.
    """
    cfg = _read_config_file(path)
    cfg = _migrate_plaintext_passwords(cfg, path)
    for key in SECURE_KEYS:
        val = get_password(key)
        if val:
            cfg[key] = val
    return cfg


def save_config_key(key, value, path=CONFIG_PATH):
    """
    Save a single key. Secure keys go to Credential Manager;
    non-sensitive keys go to the config file.
    """
    if key in SECURE_KEYS:
        set_password(key, value)
    else:
        cfg = _read_config_file(path)
        cfg = _migrate_plaintext_passwords(cfg, path)
        cfg[key] = value
        _write_config_file(cfg, path)


def update_password(key):
    """
    Interactively update a stored password (re-prompt and re-store).
    Useful when a password changes.
    """
    if key not in SECURE_KEYS:
        print("  '{}' is not a secure key.".format(key))
        return
    prompt = SECURE_PROMPTS.get(key, key)
    while True:
        val = getpass.getpass("  New {} : ".format(prompt)).strip()
        if val:
            break
        print("  Cannot be blank.")
    if set_password(key, val):
        print("  '{}' updated in Credential Manager.".format(key))
    else:
        print("  Failed to update -- keyring error.")


def reset_all_passwords():
    """Remove all Curveball passwords from Credential Manager (e.g. to re-run setup)."""
    for key in SECURE_KEYS:
        delete_password(key)
        print("  Deleted '{}'.".format(key))
    print("  All Curveball passwords cleared from Credential Manager.")


def check_status():
    """Print which credentials are stored vs missing. No values shown."""
    print("\n  Curveball Credential Status")
    print("  " + "-" * 40)
    cfg = _read_config_file()
    for key in CONFIG_KEYS:
        val = cfg.get(key, "")
        status = "[set]" if val else "[not set]"
        print("  {:25s} {} (config file)".format(key, status))
    for key in SECURE_KEYS:
        val = get_password(key)
        status = "[set]" if val else "[NOT SET -- will prompt on next run]"
        print("  {:25s} {} (Credential Manager)".format(key, status))
    print()


if __name__ == "__main__":
    # Run as a standalone utility to manage credentials
    import sys
    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        if cmd == "status":
            check_status()
        elif cmd == "reset":
            confirm = input("Reset ALL Curveball passwords from Credential Manager? (yes/no): ")
            if confirm.strip().lower() == "yes":
                reset_all_passwords()
        elif cmd == "update" and len(sys.argv) > 2:
            update_password(sys.argv[2].upper())
        else:
            print("Usage: python secure_config.py [status | reset | update <KEY>]")
            print("Keys: {}".format(", ".join(SECURE_KEYS)))
    else:
        check_status()
