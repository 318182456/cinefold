"""Stable, application-owned node pools rebuilt on every subscription refresh."""
import re

REGIONS = {
    "日本节点": r"日本|东京|東京|大阪|埼玉|🇯🇵|(?:^|[^a-z])(?:jp|jpn|japan|tokyo|osaka)(?:[^a-z]|$)",
    "香港节点": r"香港|🇭🇰|(?:^|[^a-z])(?:hk|hkg|hongkong|hong kong)(?:[^a-z]|$)",
    "台湾节点": r"台湾|台灣|台北|🇹🇼|(?:^|[^a-z])(?:tw|twn|taiwan|taipei)(?:[^a-z]|$)",
    "新加坡节点": r"新加坡|狮城|獅城|🇸🇬|(?:^|[^a-z])(?:sg|sgp|singapore)(?:[^a-z]|$)",
    "美国节点": r"美国|美國|洛杉矶|洛杉磯|纽约|紐約|🇺🇸|(?:^|[^a-z])(?:us|usa|united states|los angeles|new york)(?:[^a-z]|$)",
}
JAPAN_SOURCES = frozenset({"dmm", "mgstage", "avbase", "hbox", "avwiki", "fc2",
    "s1", "moodyz", "ideapocket", "attackers", "premium", "honnaka", "dasdas", "madonna", "fitch"})


def build_groups(nodes, custom=None):
    names = [node["name"] for node in nodes if node.get("type") not in ("direct", "reject")]
    groups = {"自动选择": names}
    matched = set()
    for group, pattern in REGIONS.items():
        members = [name for name in names if re.search(pattern, name.rsplit(" / ", 1)[-1], re.I)]
        groups[group] = members
        matched.update(members)
    groups["其他节点"] = [name for name in names if name not in matched]
    # Preserve old advanced groups without allowing them to freeze default pools.
    groups.update({name: members for name, members in (custom or {}).items() if name not in groups})
    return groups


def default_group(source_key, groups):
    return "日本节点" if (source_key in JAPAN_SOURCES or source_key.startswith("brand:")) and groups["日本节点"] else "自动选择"
