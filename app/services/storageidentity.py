"""Filesystem identity independent of a mount's temporary device number."""
import os


def filesystem_id(path) -> str | None:
    # Linux Btrfs statfs combines the filesystem UUID and subvolume ID.
    # Keep it textual: unsigned f_fsid can exceed PostgreSQL signed BIGINT.
    # Unsupported platforms retain the stricter legacy device check.
    try:
        value = os.statvfs(path).f_fsid
        return str(value) if value else None
    except (OSError, AttributeError):
        return None
