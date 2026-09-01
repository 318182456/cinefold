"""影片文件名解析：番号、分集、标记。

刮削的第一步。这里出错后面全错 —— 番号取错就抓错元数据，分集没认出来
就每集各抓一次（MDCng issue #503 的症结），标记误判就在媒体库里贴错标签
（issue #513）。

用例大多来自真实文件名，尤其是那些「看着像分集其实不是」的：
-C 是中文字幕版、-U 是无码版，都是番号的一部分，不是第 3 集第 21 集。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.utils import mediafile


class Test番号提取:
    @pytest.mark.parametrize(
        "filename,expect",
        [
            ("ABP-554.mp4", "ABP-554"),
            ("abp554.mp4", "ABP-554"),
            ("ABP-554-CD1.mp4", "ABP-554"),
            ("ABP-554-1080P-CD2.mkv", "ABP-554"),
            # 前缀带数字，不能被数字式分集规则劈开
            ("300MIUM-123.mp4", "300MIUM-123"),
            ("300MIUM-123-CD1.mp4", "300MIUM-123"),
            # 单字母前缀，_CODE_HEAD 切不了，靠快路径原样返回
            ("T28-544-CD1.mp4", "T28-544"),
            ("FC2PPV-1570936-CD2.mp4", "FC2-PPV-1570936"),
            # 日期型，下划线是官方写法的一部分
            ("032416_267.mp4", "032416_267"),
            ("032416_267-CD1.mp4", "032416_267"),
            ("", ""),
            ("random-video.mp4", ""),
        ],
    )
    def test_从文件名取番号(self, filename, expect):
        assert mediafile.parse(Path(filename)).code == expect

    def test_文件名认不出时退回父目录(self):
        # 下载来的常是 <番号>/video.mp4，只有目录名带番号
        info = mediafile.parse(Path("ABP-554/video.mp4"))
        assert info.code == "ABP-554"

    def test_父目录也带分集标记时先剥掉(self):
        info = mediafile.parse(Path("ABP-554-CD2/movie.mp4"))
        assert info.code == "ABP-554"


class Test版本后缀:
    """-C / -UC 这类后缀是番号的一部分，必须保留。

    中文字幕版与原版在媒体库里是两个独立条目，后缀丢了两版就会
    互相覆盖 —— 后刮的把先刮的产物冲掉。
    """

    @pytest.mark.parametrize(
        "filename,expect",
        [
            ("SSIS-001-C.mp4", "SSIS-001-C"),
            ("SSIS-001-UC.mp4", "SSIS-001-UC"),
            ("SSIS-001-UCH.mp4", "SSIS-001-UCH"),
            # 后缀 + 分集同时存在
            ("SSIS-001-C-CD2.mp4", "SSIS-001-C"),
            ("SSIS-001-UCH-CD1.mp4", "SSIS-001-UCH"),
            ("SSIS-001.mp4", "SSIS-001"),
        ],
    )
    def test_保留版本后缀(self, filename, expect):
        assert mediafile.parse(Path(filename)).code == expect

    @pytest.mark.parametrize(
        "filename",
        ["SSIS-001-C.mp4", "SSIS-001-CH.mp4", "SSIS-001-UC.mp4"],
    )
    def test_版本后缀不算分集(self, filename):
        assert mediafile.parse(Path(filename)).part == 0


class Test分集识别:
    @pytest.mark.parametrize(
        "filename,part",
        [
            ("ABP-554.mp4", 0),
            ("ABP-554-CD1.mp4", 1),
            ("ABP-554-cd2.mkv", 2),
            ("ABP-554-CD1-1080P.mp4", 1),
            ("ABP-554-1080P-CD2.mp4", 2),
            ("ABP-554-part1.mp4", 1),
            ("ABP-554.pt2.mp4", 2),
            # 字母式。C 跳过（那是中文字幕），所以 D 是第 4 集
            ("ABP-554-A.mp4", 1),
            ("ABP-554-B.mp4", 2),
            ("ABP-554-D.mp4", 4),
            # 纯数字式
            ("ABP-554-1.mp4", 1),
            ("ABP-554-2.mp4", 2),
            # 中文
            ("ABP-554-第1集.mp4", 1),
            ("ABP-554-上.mp4", 1),
            ("ABP-554-下.mp4", 2),
        ],
    )
    def test_分集序号(self, filename, part):
        assert mediafile.parse(Path(filename)).part == part

    @pytest.mark.parametrize(
        "filename",
        [
            # 番号自己的数字段，不是分集
            "300MIUM-123.mp4",
            "032416_267.mp4",
            # 画质残留，不是第 1080 集
            "ABP-554-1080.mp4",
            # 分集号上限之外
            "ABP-554-9.mp4",
        ],
    )
    def test_不该认成分集(self, filename):
        assert mediafile.parse(Path(filename)).part == 0

    def test_产物名带CD后缀(self):
        assert mediafile.parse(Path("ABP-554-A.mp4")).stem_for_output == "ABP-554-CD1"
        assert mediafile.parse(Path("ABP-554.mp4")).stem_for_output == "ABP-554"

    def test_is_multipart(self):
        assert mediafile.parse(Path("ABP-554-CD1.mp4")).is_multipart
        assert not mediafile.parse(Path("ABP-554.mp4")).is_multipart


class Test标记识别:
    """标记只认文件名与番号后缀里明写的。

    MDCng issue #513：没破解的片子被打上破解标签。误标比不标麻烦得多，
    用户得逐个手动改回来，所以这里宁可漏也不错。
    """

    @pytest.mark.parametrize(
        "filename",
        [
            "ABP-554 uncensored.mp4",
            "ABP-554-无码破解.mp4",
            "[破解版]ABP-554.mp4",
            "ABP-554-UC.mp4",
        ],
    )
    def test_识别无码(self, filename):
        assert mediafile.parse(Path(filename)).uncensored

    @pytest.mark.parametrize(
        "filename",
        [
            # U 单独结尾的正常番号，不能当无码
            "MIRU-001.mp4",
            "ABP-554.mp4",
            "ABP-554-1080P.mp4",
        ],
    )
    def test_不误判无码(self, filename):
        assert not mediafile.parse(Path(filename)).uncensored

    @pytest.mark.parametrize(
        "filename",
        ["[中文字幕]ABP-554.mp4", "ABP-554-中字.mp4", "ABP-554-C.mp4",
         "ABP-554 ChineseSub.mp4"],
    )
    def test_识别中文字幕(self, filename):
        assert mediafile.parse(Path(filename)).subbed

    @pytest.mark.parametrize(
        "filename",
        ["ABP-554-trailer.mp4", "ABP-554-sample.mp4", "ABP-554-预告.mp4"],
    )
    def test_识别预告片(self, filename):
        assert mediafile.parse(Path(filename)).trailer

    def test_正片不是预告片(self):
        assert not mediafile.parse(Path("ABP-554.mp4")).trailer


class Test欧美片:
    """MDCng issue #508：欧美文件名无法正确识别。

    比"认不出"更糟的是**认错** —— issue 里那个
    "VRLatina_Samy Sun_Pretty Petite_4096p_8K_LR_180" 中的 LR_180
    正好符合番号形状，会被当成 LR-180 去抓元数据，抓回来是另一部
    毫不相干的日本片，然后写进 NFO 建好硬链接。用户看到的是错的
    封面、错的演员、错的简介，而且没有任何报错。

    所以判定是欧美片就直接不提番号，让上层跳过。
    """

    @pytest.mark.parametrize(
        "filename",
        [
            # issue #508 原文里的文件名
            "VRLatina_Samy Sun_Pretty Petite_4096p_8K_LR_180.mp4",
            "Blacked.24.03.15.Some.Title.XXX.1080p.mp4",
            "Tushy.20.01.01.1080p.mp4",
            "SexArt_Ariel_Piano_1080p.mp4",
            "Brazzers - Real Wife Stories - Scene.mp4",
            "[Vixen] Title (2024-03-15).mp4",
        ],
    )
    def test_欧美片不提番号(self, filename):
        info = mediafile.parse(Path(filename))
        assert info.western
        assert info.code == ""

    @pytest.mark.parametrize(
        "filename,expect",
        [
            # 正常日本片不能被欧美判据误伤。这条尤其重要：
            # 5 个下划线段，靠「段数多」判会被误杀
            ("ABP-554_1080p_x264_AAC_中文字幕.mp4", "ABP-554"),
            ("ABP-554.mp4", "ABP-554"),
            ("ABP-554-CD1.mp4", "ABP-554"),
            ("SSIS-001-C.mp4", "SSIS-001-C"),
            ("032416_267.mp4", "032416_267"),
            ("FC2-PPV-1570936.mp4", "FC2-PPV-1570936"),
            ("300MIUM-123.mp4", "300MIUM-123"),
            ("[中文字幕]ABP-554-1080P.mp4", "ABP-554"),
        ],
    )
    def test_不误伤日本片(self, filename, expect):
        info = mediafile.parse(Path(filename))
        assert not info.western
        assert info.code == expect

    def test_looks_western直接调用(self):
        assert mediafile.looks_western("Blacked.24.03.15.Title")
        assert not mediafile.looks_western("ABP-554-1080P")
        assert not mediafile.looks_western("")


class Test按番号分组:
    def test_分集归到同一组(self):
        groups = mediafile.group_parts([
            Path("ABP-554-CD2.mp4"),
            Path("ABP-554-CD1.mp4"),
            Path("SSIS-001.mp4"),
        ])
        assert set(groups) == {"ABP-554", "SSIS-001"}
        assert [i.part for i in groups["ABP-554"]] == [1, 2]

    def test_取不出番号的归到空串组(self):
        groups = mediafile.group_parts([Path("random.mp4")])
        assert list(groups) == [""]

    def test_中文上中下按次序排(self):
        """上/中/下 撞号后重排。

        不能靠文件名排序 —— "下"的码位小于"中"，按名字排会得到
        上/下/中，重排后中下颠倒。
        """
        groups = mediafile.group_parts([
            Path("ABP-554-上.mp4"), Path("ABP-554-中.mp4"), Path("ABP-554-下.mp4"),
        ])
        items = groups["ABP-554"]
        assert [i.path.name for i in items] == [
            "ABP-554-上.mp4", "ABP-554-中.mp4", "ABP-554-下.mp4",
        ]
        assert [i.part for i in items] == [1, 2, 3]

    def test_混合命名撞号时重排(self):
        groups = mediafile.group_parts([
            Path("ABP-554-CD1.mp4"), Path("ABP-554-B.mp4"),
        ])
        # CD1 与 B 都判成各自的序号，撞不撞号取决于取值；
        # 不管怎样最终序号必须互不相同
        parts = [i.part for i in groups["ABP-554"]]
        assert len(set(parts)) == len(parts)

    def test_不撞号时不动序号(self):
        """CD1 + CD3（第二集缺失）是合法状态，不该被压成 CD1+CD2。"""
        groups = mediafile.group_parts([
            Path("ABP-554-CD1.mp4"), Path("ABP-554-CD3.mp4"),
        ])
        assert [i.part for i in groups["ABP-554"]] == [1, 3]
