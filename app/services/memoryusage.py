"""Read process memory without walking the Python heap or forcing collections."""
import os
import time
from pathlib import Path


def snapshot():
    result = {'pid': os.getpid(), 'sampled_at': time.time()}
    try:
        fields = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
        for source, name in (('VmRSS', 'rss'), ('VmHWM', 'peak_rss'), ('RssAnon', 'anonymous'), ('RssFile', 'mapped_files')):
            result[name] = int(fields[source].split()[0])*1024
    except (OSError, ValueError, KeyError):
        pass
    return result
