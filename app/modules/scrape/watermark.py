"""按明确的文件标记添加角标，预览与落盘共用同一绘制函数。"""
from __future__ import annotations

import math
import re
from dataclasses import replace
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from app.modules.scrape.preferences import category, sites

DEFAULTS = {
    "enabled": False, "targets": "poster,thumb",
    "types": "sub,crack,leak,uncensored,censored,resolution",
    "layout": "stack", "corner": "top_left", "scale": 0.07,
    "offset_x": 0.015, "offset_y": 0.015, "gap": 0.01,
    **{f"position_{key}": "auto" for key in ("sub", "crack", "leak", "uncensored", "censored", "resolution")},
}
CORNERS = ("top_left", "top_right", "bottom_right", "bottom_left")
BADGES = {
    "sub": ("字幕", "SUB", "#27ad58"), "crack": ("破解", "CRACK", "#df2828"),
    "leak": ("流出", "LEAK", "#dd6b20"), "uncensored": ("无码", "UNC", "#6554c0"),
    "censored": ("有码", "CENS", "#237bc0"), "4K": ("4K", "4K", "#caa400"), "8K": ("8K", "8K", "#caa400"),
}


def options(raw) -> dict:
    return {**DEFAULTS, **(raw if isinstance(raw, dict) else {})}


def validate(raw) -> str:
    if not isinstance(raw, dict) or set(raw) - DEFAULTS.keys():
        return "水印配置格式或字段无效"
    opts = options(raw)
    if not isinstance(opts["enabled"], bool):
        return "水印启用状态须为布尔值"
    if set(sites(opts["targets"])) - {"poster", "thumb", "fanart"} or set(sites(opts["types"])) - {"sub", "crack", "leak", "uncensored", "censored", "resolution"}:
        return "水印图片或标记类型无效"
    if opts["corner"] not in CORNERS or opts["layout"] not in ("stack", "clockwise", "counterclockwise"):
        return "水印布局或起始位置无效"
    for key in BADGES.keys() - {"4K", "8K"} | {"resolution"}:
        if opts[f"position_{key}"] not in (*CORNERS, "auto"):
            return "水印固定位置无效"
    for key, low, high in (("scale", 0.02, 0.2), ("offset_x", 0, 0.2), ("offset_y", 0, 0.2), ("gap", 0, 0.1)):
        try:
            value = float(opts[key])
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError
        except (ValueError, TypeError):
            return f"水印 {key} 必须在 {low}～{high} 之间"
    return ""


def labels(info) -> list[str]:
    text = str(info.path).lower()
    result = ["sub"] if info.subbed else []
    # 类型逐级覆盖：破解 > 流出 > 原生无码 > 有码。
    leak = bool(re.search(r"流出|(?<![a-z])leak(?:ed)?(?![a-z])", text))
    crack = bool(re.search(r"破解|(?<![a-z])crack(?:ed)?(?![a-z])", text))
    if crack or info.uncensored and not leak and category(info.code) not in ("uncensored", "fc2"):
        result.append("crack")
    elif leak:
        result.append("leak")
    elif category(info.code) in ("uncensored", "fc2"):
        result.append("uncensored")
    elif category(info.code) in ("censored", "amateur"):
        result.append("censored")
    if re.search(r"(?<![a-z0-9])(?:8k|4320p)(?![a-z0-9])", text):
        result.append("8K")
    elif re.search(r"(?<![a-z0-9])(?:4k|2160p)(?![a-z0-9])", text):
        result.append("4K")
    return result


def _font(size):
    for path in ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", "C:/Windows/Fonts/msyh.ttc", "/System/Library/Fonts/PingFang.ttc"):
        try:
            return ImageFont.truetype(path, size), True
        except OSError:
            continue
    return ImageFont.load_default(size=size), False


def render(data: bytes, detected: list[str], raw, kind: str) -> bytes:
    opts = options(raw)
    if not opts["enabled"] or kind not in sites(opts["targets"]) or validate(opts):
        return data
    allowed = set(sites(opts["types"]))
    selected = [x for x in detected if x in BADGES and ("resolution" if x in ("4K", "8K") else x) in allowed]
    if not selected or not data:
        return data
    try:
        with Image.open(BytesIO(data)) as source:
            image = source.convert("RGB")
        width, height = image.size
        badge_h = max(12, round(min(width, height) * float(opts["scale"])))
        font, chinese = _font(max(9, round(badge_h * 0.62)))
        margin_x, margin_y = round(width * float(opts["offset_x"])), round(height * float(opts["offset_y"]))
        gap = max(1, round(min(width, height) * float(opts["gap"])))
        draw = ImageDraw.Draw(image)
        cursors = {corner: [0, 0, 0] for corner in CORNERS}  # 横向位移、纵向位移、当前行高
        start = CORNERS.index(opts["corner"])
        for index, key in enumerate(selected):
            text, fallback, color = BADGES[key]
            text = text if chinese else fallback
            bbox = draw.textbbox((0, 0), text, font=font)
            badge_w = min(width - 2 * margin_x, bbox[2] - bbox[0] + badge_h)
            poskey = "resolution" if key in ("4K", "8K") else key
            corner = opts[f"position_{poskey}"]
            if corner == "auto":
                step = -index if opts["layout"] == "counterclockwise" else index if opts["layout"] == "clockwise" else 0
                corner = CORNERS[(start + step) % 4]
            cursor = cursors[corner]
            if cursor[0] and cursor[0] + badge_w > width - 2 * margin_x:
                cursor[0] = 0
                cursor[1] += cursor[2] + gap
                cursor[2] = 0
            x = margin_x + cursor[0] if corner.endswith("left") else width - margin_x - cursor[0] - badge_w
            y = margin_y + cursor[1] if corner.startswith("top") else height - margin_y - cursor[1] - badge_h
            draw.rounded_rectangle((x, y, x + badge_w, y + badge_h), radius=badge_h // 3, fill=color)
            draw.text((x + badge_w / 2, y + badge_h / 2), text, font=font, fill="white", anchor="mm")
            cursor[0] += badge_w + gap
            cursor[2] = max(cursor[2], badge_h)
        result = BytesIO()
        image.save(result, format="JPEG", quality=92)
        image.close()
        return result.getvalue()
    except (OSError, ValueError):
        return data


def apply(images, info, raw):
    detected = labels(info)
    return replace(images, poster=render(images.poster, detected, raw, "poster"),
                   fanart=render(images.fanart, detected, raw, "fanart"),
                   thumb=render(images.fanart, detected, raw, "thumb"))
