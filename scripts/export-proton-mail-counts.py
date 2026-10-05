#!/usr/bin/env python3
"""Export read-only Proton Mail Bridge folder counts for Home Assistant.

Uses IMAP STATUS, which reports counts without selecting a mailbox, so no
message flags or contents are read or changed. A command allowlist keeps the
client read-only even though the Bridge password itself allows writes.
"""

import argparse
import imaplib
import json
import os
import re
import ssl
import sys
import tempfile
import time
from pathlib import Path


HOST = "127.0.0.1"
PORT = 1143
STATUS_RE = re.compile(rb"\((?P<items>[^()]*)\)\s*$")
ALLOWED_COMMANDS = {"CAPABILITY", "STARTTLS", "LOGIN", "LIST", "STATUS", "LOGOUT"}


class ReadOnlyIMAP(imaplib.IMAP4):
    def _command(self, name, *args):
        if name not in ALLOWED_COMMANDS:
            raise ValueError(f"IMAP command {name} is outside the read-only allowlist")
        return super()._command(name, *args)


def required_path(name):
    value = os.environ.get(name)
    if not value:
        raise ValueError(f"{name} is missing")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(f"{name} must be an absolute path")
    return path


def read_secret(name):
    value = required_path(name).read_text(encoding="utf-8").strip()
    if not value or any(c in value for c in ("\r", "\n", "\x00")):
        raise ValueError(f"{name} does not contain a single-line value")
    return value


def mailboxes():
    """Parse PROTON_MAIL_COUNTS_MAILBOXES, e.g. "inbox=INBOX;todo=Folders/Todo"."""
    value = os.environ.get("PROTON_MAIL_COUNTS_MAILBOXES")
    if not value:
        raise ValueError("PROTON_MAIL_COUNTS_MAILBOXES is missing")
    result = {}
    for entry in value.split(";"):
        key, sep, mailbox = entry.partition("=")
        key, mailbox = key.strip(), mailbox.strip()
        if not sep or not re.fullmatch(r"[a-z][a-z0-9_]*", key) or not mailbox or key in result:
            raise ValueError(f"Invalid PROTON_MAIL_COUNTS_MAILBOXES entry: {entry!r}")
        if not mailbox.isascii() or any(c in mailbox for c in ("\r", "\n", "\x00")):
            raise ValueError(f"Mailbox for {key} must be its ASCII IMAP name as shown by --list")
        result[key] = mailbox
    return result


def quote(mailbox):
    return '"' + mailbox.replace("\\", "\\\\").replace('"', '\\"') + '"'


def connect():
    username = read_secret("PROTON_MAIL_COUNTS_USERNAME_FILE")
    password = read_secret("PROTON_MAIL_COUNTS_PASSWORD_FILE")
    # Trust only the certificate copied from c3's local Bridge listener.
    context = ssl.create_default_context(cafile=required_path("PROTON_MAIL_COUNTS_CA_FILE"))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    client = ReadOnlyIMAP(HOST, PORT, timeout=30)
    try:
        client.starttls(ssl_context=context)
        client.login(username, password)
    except Exception:
        try:
            client.logout()
        except Exception:
            pass
        raise
    return client


def status(client, mailbox):
    typ, data = client.status(quote(mailbox), "(MESSAGES UNSEEN)")
    if typ != "OK" or not data or not isinstance(data[0], bytes):
        raise RuntimeError(f"STATUS failed for {mailbox}")
    match = STATUS_RE.search(data[0])
    if not match:
        raise RuntimeError(f"Unexpected STATUS response for {mailbox}")
    items = match.group("items").split()
    values = {k.decode().upper(): int(v) for k, v in zip(items[::2], items[1::2])}
    return values["MESSAGES"], values["UNSEEN"]


def list_mailboxes(client):
    typ, data = client.list()
    if typ != "OK":
        raise RuntimeError("LIST failed")
    for row in data:
        if isinstance(row, bytes):
            print(row.decode("ascii", errors="replace"))


def export(client):
    output = required_path("PROTON_MAIL_COUNTS_OUTPUT")
    counts = {}
    for key, mailbox in mailboxes().items():
        counts[f"{key}_messages"], counts[f"{key}_unseen"] = status(client, mailbox)
    counts["sampled_at"] = int(time.time())

    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=output.parent, delete=False) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            json.dump(counts, handle, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary, output)
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true", help="print IMAP mailbox names and exit")
    args = parser.parse_args()
    client = connect()
    try:
        list_mailboxes(client) if args.list else export(client)
    finally:
        client.logout()


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, ValueError, RuntimeError, imaplib.IMAP4.error, ssl.SSLError) as exc:
        print(f"Proton Mail count export failed: {exc}", file=sys.stderr)
        sys.exit(1)
