import json
import os
import threading
from datetime import datetime


_AUDIT_LOCK = threading.Lock()


def append_audit(event, payload):
    audit_log = os.getenv("CXXCRAFTER_AUDIT_LOG")
    if not audit_log:
        return

    log_dir = os.path.dirname(audit_log)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)

    timestamp = datetime.now().isoformat()
    with _AUDIT_LOCK:
        with open(audit_log, "a", encoding="utf-8") as f:
            f.write(f"\n===== CXXCRAFTER_AUDIT {timestamp} {event} =====\n")
            f.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            f.write("\n")
