"""Audit logging for VMware AVI operations.

All write operations are logged to ~/.vmware-avi/audit.log in JSON Lines format.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from vmware_avi.config import CONFIG_DIR

AUDIT_LOG = CONFIG_DIR / "audit.log"
_log = logging.getLogger("vmware-avi.audit")


def log_operation(
    operation: str,
    resource: str,
    parameters: dict | None = None,
    result: str = "success",
    user: str = "",
) -> None:
    """Append an audit entry to the audit log."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        "resource": resource,
        "parameters": parameters or {},
        "result": result,
        "user": user,
    }

    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as exc:
        _log.warning("Failed to write audit log: %s", exc)
