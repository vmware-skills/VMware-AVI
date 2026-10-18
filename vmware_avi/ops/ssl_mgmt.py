"""SSL certificate management operations."""

from __future__ import annotations

from datetime import datetime, timezone

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from vmware_avi._safety import sanitize
from vmware_avi.config import load_config
from vmware_avi.connection import AviConnectionManager, api_get_all

console = Console()


def list_certificates() -> None:
    """List all SSL certificates."""
    cfg = load_config()
    mgr = AviConnectionManager(cfg)
    session = mgr.connect()

    certs = api_get_all(session, "sslkeyandcertificate")

    table = Table(title="SSL Certificates")
    table.add_column("Name")
    table.add_column("Subject")
    table.add_column("Expiry")
    table.add_column("Type")

    for cert in certs:
        name = sanitize(cert.get("name", ""))
        cert_info = cert.get("certificate", {})
        subject = sanitize(cert_info.get("subject", {}).get("common_name", "N/A"))
        not_after = cert_info.get("not_after", "N/A")
        cert_type = cert.get("type", "N/A")
        table.add_row(name, subject, not_after, cert_type)

    console.print(table)
    console.print(f"  Total: {len(certs)} certificate(s)")


def check_expiry(days: int = 30) -> None:
    """Check certificates expiring within N days."""
    cfg = load_config()
    mgr = AviConnectionManager(cfg)
    session = mgr.connect()

    # Expiry filtering stays client-side — AVI has no server-side "not_after
    # within N days" filter — but we page through the full cert collection so
    # the "N expiring" tally is honest in large environments.
    certs = api_get_all(session, "sslkeyandcertificate")

    now = datetime.now(timezone.utc)
    expiring: list[dict] = []
    # A certificate whose expiry cannot be read is not "not expiring": counted
    # and named below, never silently dropped from the tally.
    unread: list[str] = []

    for cert in certs:
        cert_info = cert.get("certificate", {}) or {}
        not_after_str = cert_info.get("not_after", "")
        if not not_after_str:
            unread.append(sanitize(cert.get("name", "")))
            continue

        try:
            not_after = datetime.strptime(not_after_str, "%Y-%m-%d %H:%M:%S")
            not_after = not_after.replace(tzinfo=timezone.utc)
        except ValueError:
            unread.append(sanitize(cert.get("name", "")))
            continue

        days_left = (not_after - now).days
        if days_left <= days:
            expiring.append(
                {
                    "name": cert.get("name", ""),
                    "not_after": not_after_str,
                    "days_left": days_left,
                }
            )

    table = Table(title=f"Certificates Expiring Within {days} Days")
    table.add_column("Name")
    table.add_column("Expiry Date")
    table.add_column("Days Left")

    for e in sorted(expiring, key=lambda x: x["days_left"]):
        style = "red" if e["days_left"] < 0 else "yellow"
        table.add_row(e["name"], e["not_after"], f"[{style}]{e['days_left']}[/{style}]")

    console.print(table)
    console.print(f"  {len(expiring)} certificate(s) expiring within {days} days.")
    if unread:
        console.print(
            f"  [yellow]{len(unread)} certificate(s) whose expiry could not be read (missing or "
            f"unrecognised not_after), so they are not in the count above: "
            f"{escape(', '.join(unread[:20]))}[/yellow]"
        )
