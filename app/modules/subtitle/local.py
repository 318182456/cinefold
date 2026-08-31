"""本地字幕库。

手工下载来的字幕丢进一个目录，按番号自动认领。目录配在
SUBTITLE_LOCAL_DIR，空表示不启用。

排在网络源之前：本地命中就不必跨境请求了，比任何网络源都快且稳。
适合那些站点不给自动抓、只能人工下载的字幕 —— 下完丢进来，
下一轮补漏自己就用上了。

文件只读不删。目录是你的素材库，媒体库那边的影片日后改名、重建硬链接
都还能再取一次；命中即删的话那些场景就没有源文件可用了。
"""
from __future__ import annotations

import re
from pathlib import Path

from loguru import logger

from app.modules.subtitle.base import (
    MAX_SUBTITLE_BYTES,
    SUBTITLE_SUFFIXES,
    SubtitleItem,
    as_simplified_chinese,
    decode_subtitle,
    looks_like_subtitle,
)
from app.utils import get_true_code

# 版本后缀。番号后面挂着这些时仍是同一部片的字幕：
# -C/-CH 中文版、-U/-UC 无码版、分卷与画质标记。
# get_true_code 会保留 -C/-UC/-U（它们算番号的一部分），
# 所以剥离得在这里自己做
_VERSION_SUFFIX = re.compile(
    r"-(?:C|CH|CHS|CHT|U|UC|UNCEN(?:SORED)?|CD\d*|PART\d*|\d{3,4}P|4K)$",
    re.IGNORECASE,
)


def _strip_version(code: str) -> str:
    """去掉番号末尾的版本后缀。剥不掉就原样返回。

    连着剥：SSNI-497-UC-CD1 这种叠了两层的也要还原成 SSNI-497。
    """
    out = code
    while True:
        stripped = _VERSION_SUFFIX.sub("", out)
        if stripped == out or not stripped:
            return out
        out = stripped


class LocalSubtitle:
    name = "subtitlelocal"

    def __init__(self, directory: str = ""):
        if not directory:
            from app.core.config import get_settings

            directory = get_settings().subtitle_local_dir

        self.directory = Path(directory) if directory else None
        # 番号 → 文件路径。第一次 search 时建，之后整轮复用 ——
        # 目录里可能有几千个文件，每部片都遍历一遍太浪费。
        # 不做失效：实例只在单轮补漏里活着，缓存活得越久读到的目录越旧
        self._index: dict[str, Path] | None = None

    def search(self, code: str) -> SubtitleItem | None:
        code = get_true_code(code)
        if not code:
            return None

        index = self._ensure_index()
        if not index:
            return None

        path = index.get(code)
        if path is None:
            # 精确没中，再试剥掉版本后缀的形态：媒体库里是 SSNI-497-C，
            # 素材库里可能只放了 SSNI-497 的字幕，那是同一部片
            path = index.get(_strip_version(code))
        if path is None:
            return None

        content = self._read(path)
        if not content:
            return None

        return SubtitleItem(
            code=code,
            title=path.name,
            site=self.name,
            content=content,
            suffix=path.suffix.lower(),
        )

    def _ensure_index(self) -> dict[str, Path]:
        if self._index is None:
            self._index = self._build_index()
        return self._index

    def _build_index(self) -> dict[str, Path]:
        """扫目录，建「番号 → 文件」索引。

        递归子目录 —— 素材库常按厂牌或下载日期分文件夹，强求平铺不现实。

        同一番号有多个文件时留先遇到的。挑不出哪个更好（都是人工下载的），
        与其定一套猜测规则，不如行为可预期：想换就把不要的移出目录。
        """
        directory = self.directory
        if directory is None:
            return {}

        try:
            if not directory.is_dir():
                logger.warning(f"[字幕] 本地字幕库目录不存在: {directory}")
                return {}
        except OSError as exc:
            logger.warning(f"[字幕] 本地字幕库目录读不了: {exc}")
            return {}

        index: dict[str, Path] = {}
        count = 0
        try:
            for path in directory.rglob("*"):
                if path.suffix.lower() not in SUBTITLE_SUFFIXES:
                    continue
                try:
                    if not path.is_file():
                        continue
                except OSError:
                    continue

                count += 1
                code = self._code_of(path)
                if code and code not in index:
                    index[code] = path
        except OSError as exc:
            # 扫到一半失败（权限、网络盘掉线）时用已经建好的部分，
            # 有多少算多少，比整个源当不可用强
            logger.warning(f"[字幕] 扫本地字幕库中断: {exc}")

        logger.info(
            f"[字幕] 本地字幕库 {directory} 收录 {len(index)} 个番号"
            f"（扫到 {count} 个字幕文件）"
        )
        return index

    @staticmethod
    def _code_of(path: Path) -> str:
        """从文件名认出番号。认不出返回空串。

        文件名常带下载来源的噪声（「SSNI-497（迅雷）.srt」、
        「SSNI-497 (1).ass」），get_true_code 会把番号后面的尾巴切掉。
        """
        return get_true_code(path.stem)

    def _read(self, path: Path) -> str:
        """读一个字幕文件，规整成简体。不可用时返回空串。

        与网络源同样要按字节猜编码：人工下载来的文件 GBK、BIG5 都有，
        按 UTF-8 硬读会出乱码。繁体在这里一并转成简体。
        """
        try:
            raw = path.read_bytes()
        except OSError as exc:
            logger.warning(f"[字幕] 读本地字幕失败 {path}: {exc}")
            return ""

        # 上限照旧防离谱文件；下限不套网络源那道 MIN_SUBTITLE_BYTES ——
        # 那是用来挡站点返回的空壳错误页的，本地文件是人工放进来的，
        # 不存在那种情况，真短的字幕不该被无谓拒掉。
        # 内容像不像字幕由下面 looks_like_subtitle 把关
        if not raw or len(raw) > MAX_SUBTITLE_BYTES:
            logger.debug(f"[字幕] 本地字幕体积不合常理，跳过: {path}")
            return ""

        text = decode_subtitle(raw)
        if not looks_like_subtitle(text):
            logger.debug(f"[字幕] 本地文件不像字幕，跳过: {path}")
            return ""

        content = as_simplified_chinese(text)
        if not content:
            logger.debug(f"[字幕] 本地字幕不是中文，跳过: {path}")
        return content
