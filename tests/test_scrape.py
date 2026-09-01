"""自建刮削：NFO 生成、命名模板、图片落盘、多源合并。

替代外部刮削工具（MDCng 等）那条链路。这里覆盖的是产物正确性 ——
产物一旦写进媒体库就长期躺在那儿，Emby 读到什么全看这几个模块。

几处对着 MDCng 的已知问题写的用例，都标了 issue 号。
"""
from __future__ import annotations

import io
from pathlib import Path
from xml.etree import ElementTree

import pytest

from app.modules.scrape import merge, naming, nfo

Image = pytest.importorskip("PIL.Image", reason="未安装 Pillow")

from app.modules.scrape import images as scrape_images  # noqa: E402  需在 Pillow 检查之后


def _encode(width: int, height: int, quality: int = 85, split: bool = False) -> bytes:
    """造一张图。split=True 时左半红右半蓝，用来验证裁的是哪半边。"""
    image = Image.new("RGB", (width, height), (120, 60, 60))
    if split:
        for x in range(width):
            color = (255, 0, 0) if x < width // 2 else (0, 0, 255)
            for y in range(height):
                image.putpixel((x, y), color)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


# ----------------------------------------------------------------------
class TestNFO生成:
    def test_完整字段(self):
        data = nfo.NfoData(
            code="ABS-001", title="Original", cn_title="中文标题",
            release_date="2024-03-15", duration="120分钟", producer="プレステージ",
            publisher="ABS", series="テスト系列", genres="単体作品,巨乳",
            casts="深田えいみ,三上悠亜", star=8.5,
        )
        root = ElementTree.fromstring(nfo.render(data))
        assert root.findtext("title") == "ABS-001 中文标题"
        assert root.findtext("originaltitle") == "Original"
        assert root.findtext("year") == "2024"
        assert root.findtext("runtime") == "120"
        assert root.findtext("rating") == "8.5"
        assert [e.text for e in root.findall("genre")] == ["単体作品", "巨乳"]
        assert [e.findtext("name") for e in root.findall("actor")] == [
            "深田えいみ", "三上悠亜",
        ]

    def test_字段缺失时不写空节点(self):
        """空节点会让部分 Emby 版本显示成空字符串，比字段缺失更难看。"""
        root = ElementTree.fromstring(nfo.render(nfo.NfoData(code="ABS-001")))
        assert root.find("studio") is None
        assert root.find("rating") is None
        assert root.findtext("title") == "ABS-001"

    def test_分集标题带CD后缀(self):
        data = nfo.NfoData(code="ABS-001", cn_title="标题", part=2)
        root = ElementTree.fromstring(nfo.render(data))
        assert root.findtext("title") == "ABS-001 标题 CD2"
        assert root.findtext("sorttitle") == "ABS-001-CD2"

    def test_清掉非法XML字符(self):
        """抓来的文本里混进控制字符时，整份 NFO 会解析不了 ——
        而 Emby 只在自己的日志里报错，表现为「没有元数据」，很难查。"""
        data = nfo.NfoData(code="ABS-001", cn_title="标题\x00\x08带控制字符")
        rendered = nfo.render(data)
        root = ElementTree.fromstring(rendered)  # 不抛异常即可
        assert "\x00" not in root.findtext("title")

    def test_官方简介进plot(self):
        """简介来自 airav（唯一给这个字段的源），进 NFO 的 <plot>。

        AI 看点会拼在它前面，靠 review.py 的 MARKER/END_MARKER
        做可重复替换，不会把官方简介冲掉。
        """
        data = nfo.NfoData(code="ABS-001", plot="官方剧情简介。")
        root = ElementTree.fromstring(nfo.render(data))
        assert root.findtext("plot") == "官方剧情简介。"
        assert root.findtext("outline") == "官方剧情简介。"

    def test_无简介时不写plot节点(self):
        """绝大多数番号没有简介。空节点比缺失更难看，
        而且绝不能拿标题兜底 —— 那等于同一句话写两遍。"""
        data = nfo.NfoData(code="ABS-001", cn_title="中文标题", plot="")
        root = ElementTree.fromstring(nfo.render(data))
        assert root.find("plot") is None
        assert root.findtext("title") == "ABS-001 中文标题"

    def test_类别繁转简(self):
        """实测 SSIS-001 抓回来的类别是繁体（薄馬賽克/高畫質/戲劇）。

        Emby 按标签分类浏览，繁简混着会分成两个类目 ——
        「戲劇」和「戏剧」各一个，点进去各有一半片子。
        """
        data = nfo.NfoData(code="ABS-001", genres="薄馬賽克,高畫質,戲劇")
        root = ElementTree.fromstring(nfo.render(data))
        assert [e.text for e in root.findall("genre")] == [
            "薄马赛克", "高画质", "戏剧",
        ]

    def test_繁简同名标签去重(self):
        data = nfo.NfoData(code="ABS-001", genres="戲劇,戏剧")
        root = ElementTree.fromstring(nfo.render(data))
        assert [e.text for e in root.findall("genre")] == ["戏剧"]

    def test_演员名不做繁转简(self):
        """人名不该动 —— 日文名转了反而错。"""
        data = nfo.NfoData(code="ABS-001", casts="鈴村あいり,葵つかさ")
        root = ElementTree.fromstring(nfo.render(data))
        assert [e.findtext("name") for e in root.findall("actor")] == [
            "鈴村あいり", "葵つかさ",
        ]

    def test_标记进标签(self):
        data = nfo.NfoData(code="ABS-001", extra_genres=["中文字幕", "无码破解"])
        root = ElementTree.fromstring(nfo.render(data))
        genres = [e.text for e in root.findall("genre")]
        assert "中文字幕" in genres and "无码破解" in genres

    @pytest.mark.parametrize(
        "raw,expect",
        [
            ("120分钟", "120"), ("120 min", "120"), ("02:00:00", "120"),
            ("1:30", "90"), ("120", "120"),
            # 认不出来的一律不写，别瞎猜
            ("abc", ""), ("99999", ""), ("", ""),
        ],
    )
    def test_时长解析(self, raw, expect):
        assert nfo._runtime_minutes(raw) == expect

    def test_写盘与不覆盖(self, tmp_path):
        target = tmp_path / "ABS-001.nfo"
        assert nfo.write(target, nfo.NfoData(code="ABS-001"))
        assert target.is_file()

        # overwrite=False 时保留用户手改过的内容
        target.write_text("手改过的", encoding="utf-8")
        assert not nfo.write(target, nfo.NfoData(code="ABS-001"), overwrite=False)
        assert target.read_text(encoding="utf-8") == "手改过的"


# ----------------------------------------------------------------------
class Test命名模板:
    def _ctx(self, **kwargs):
        base = dict(
            number="ABS-001", category="日本AV", actor="深田えいみ,三上悠亜",
            series="テスト系列", title="中文标题", studio="プレステージ",
            source_path="/downloads/ABS-001.mp4",
        )
        base.update(kwargs)
        return naming.NamingContext(**base)

    @pytest.mark.parametrize(
        "template,expect",
        [
            ("{category}/{first_actor}", "日本AV/深田えいみ"),
            ("{{ category }}/{{ first_actor }}", "日本AV/深田えいみ"),
            ("{% if series %}{{ series }}/{% endif %}{{ number }}", "テスト系列/ABS-001"),
            ("{{ number | upper }}/{{ first_letter }}", "ABS-001/A"),
        ],
    )
    def test_目录模板(self, template, expect):
        got = naming.render_dir(template, self._ctx())
        assert got.as_posix() == expect

    def test_split_filter(self):
        """Jinja2 没有内置 split，但 MDCng 的模板文档里列了它，
        用户照抄过来必须能跑。"""
        pytest.importorskip("jinja2")
        got = naming.render_dir(
            '{{ category }}/{{ number | split("-") | first }}/{{ number }}',
            self._ctx(),
        )
        assert got.as_posix() == "日本AV/ABS/ABS-001"

    def test_多演员不拆目录(self):
        """MDCng issue #510：多演员会被拆成两个目录。
        actor 给全部演员，要单个的用 first_actor —— 由模板决定。"""
        got = naming.render_dir("{category}/{actor}", self._ctx())
        assert got.as_posix() == "日本AV/深田えいみ,三上悠亜"

    def test_基础语法缺失填未知(self):
        got = naming.render_dir("{category}/{first_actor}", self._ctx(actor=""))
        assert got.as_posix() == "日本AV/未知"

    def test_分集字段为空不填未知(self):
        """不分集的片子本来就没有分集号，填「未知」会得到 ABS-001未知.mp4。"""
        got = naming.render_file("{number}{cd}", self._ctx(), ".mp4")
        assert got == "ABS-001.mp4"

    def test_自动补分集后缀(self):
        got = naming.render_file("{number}", self._ctx(part=2), ".mp4")
        assert got == "ABS-001-CD2.mp4"

    def test_模板已写分集则不重复补(self):
        got = naming.render_file("{number}{cd}", self._ctx(part=2), ".mp4")
        assert got == "ABS-001-CD2.mp4"

    def test_源文件名已带CD不重复补(self):
        """{source_filename} 渲染出 ABS-001-CD2-1080P 时，
        末尾再补一次就成了 -CD2-1080P-CD2。"""
        got = naming.render_file(
            "{source_filename}",
            self._ctx(part=2, source_path="/d/ABS-001-CD2-1080P.mp4"),
            ".mp4",
        )
        assert got == "ABS-001-CD2-1080P.mp4"

    def test_清掉空字段残留的分隔符(self):
        got = naming.render_file("{{ number }}-{{ resolution }}", self._ctx(), ".mp4")
        assert got == "ABS-001.mp4"

    @pytest.mark.parametrize(
        "raw,expect",
        [
            ("a/b", "a_b"),          # 路径分隔符换掉而不是删掉
            ("a<b>c", "a_b_c"),
            ("尾部点.", "尾部点"),      # Windows 上以点结尾的目录打不开
            ("尾部空格 ", "尾部空格"),
            ("CON", "CON_"),          # Windows 保留设备名
        ],
    )
    def test_清洗路径片段(self, raw, expect):
        assert naming.sanitize_segment(raw) == expect

    def test_超长截断(self):
        got = naming.sanitize_segment("长" * 200)
        assert len(got) <= naming.MAX_SEGMENT

    def test_模板渲染为空时退回番号(self):
        got = naming.render_dir("{{ nonexistent_field }}", self._ctx())
        assert got.as_posix() == "ABS-001"

    @pytest.mark.parametrize("template", ["", "   "])
    def test_目录模板留空表示平铺(self, template):
        """留空是有意义的配置：产物直接落在根目录下，不分子目录。

        不能回退到默认模板 —— 用户特意清空就是想要平铺，
        悄悄给他建一层 "日本AV/未知" 是帮倒忙。
        """
        assert naming.render_dir(template, self._ctx()) == Path(".")


# ----------------------------------------------------------------------
class Test海报裁切:
    def test_横版双拼裁成竖版(self):
        out = scrape_images.crop_poster(_encode(800, 538), "RIGHT")
        with Image.open(io.BytesIO(out)) as image:
            width, height = image.size
        assert width / height == pytest.approx(scrape_images.POSTER_RATIO, abs=0.01)

    @pytest.mark.parametrize(
        "side,expect_red",
        [("LEFT", True), ("RIGHT", False), ("", False)],
    )
    def test_按人像面裁对应半边(self, side, expect_red):
        """portrait_side 为空时靠右裁 —— 左碟片右人像是通行版式，
        与 imgcrop.detect_portrait_side 的兜底一致。"""
        out = scrape_images.crop_poster(_encode(800, 538, split=True), side)
        with Image.open(io.BytesIO(out)) as image:
            pixel = image.getpixel((image.size[0] // 2, image.size[1] // 2))
        assert (pixel[0] > 150) is expect_red

    @pytest.mark.parametrize("width,height", [(400, 600), (500, 500)])
    def test_竖版与方形不动(self, width, height):
        src = _encode(width, height)
        assert scrape_images.crop_poster(src, "RIGHT") == src

    def test_坏图原样返回(self):
        assert scrape_images.crop_poster(b"not an image", "RIGHT") == b"not an image"


class Test图片落盘:
    def test_命名带影片名前缀(self, tmp_path):
        """一个目录里可能放多部片（按演员归档时常见），
        目录级的 poster.jpg 会互相覆盖。"""
        video = tmp_path / "ABS-001.mp4"
        video.write_bytes(b"x")
        cover = _encode(800, 538)
        got = scrape_images.write_images(
            video, scrape_images.build_image_set(cover, [], "RIGHT")
        )
        assert got["poster"] == "ABS-001-poster.jpg"
        assert (tmp_path / "ABS-001-poster.jpg").is_file()
        assert (tmp_path / "ABS-001-fanart.jpg").is_file()
        assert (tmp_path / "ABS-001-thumb.jpg").is_file()

    def test_分集各自一份图(self, tmp_path):
        """MDCng issue #503：带 cd 分片的影片刮不到剧照。
        每个分集旁边各写一份，Emby 才能按文件名配对上。"""
        cover = _encode(800, 538)
        image_set = scrape_images.build_image_set(cover, [], "RIGHT")
        for stem in ("ABS-001-CD1", "ABS-001-CD2"):
            video = tmp_path / f"{stem}.mp4"
            video.write_bytes(b"x")
            scrape_images.write_images(video, image_set)
        for stem in ("ABS-001-CD1", "ABS-001-CD2"):
            assert (tmp_path / f"{stem}-poster.jpg").is_file()

    def test_剧照进extrafanart(self, tmp_path):
        video = tmp_path / "ABS-001.mp4"
        video.write_bytes(b"x")
        stills = [_encode(400, 300) for _ in range(3)]
        scrape_images.write_images(
            video, scrape_images.build_image_set(_encode(800, 538), stills, "")
        )
        directory = tmp_path / scrape_images.EXTRAFANART_DIR
        assert sorted(p.name for p in directory.iterdir()) == [
            "1.jpg", "2.jpg", "3.jpg",
        ]

    def test_剧照数量上限(self, tmp_path):
        video = tmp_path / "ABS-001.mp4"
        video.write_bytes(b"x")
        stills = [_encode(400, 300) for _ in range(scrape_images.MAX_STILLS + 5)]
        scrape_images.write_images(
            video, scrape_images.build_image_set(_encode(800, 538), stills, "")
        )
        directory = tmp_path / scrape_images.EXTRAFANART_DIR
        assert len(list(directory.iterdir())) == scrape_images.MAX_STILLS

    def test_不覆盖已有图片(self, tmp_path):
        video = tmp_path / "ABS-001.mp4"
        video.write_bytes(b"x")
        poster = tmp_path / "ABS-001-poster.jpg"
        poster.write_bytes("用户自己换的图".encode("utf-8"))
        scrape_images.write_images(
            video,
            scrape_images.build_image_set(_encode(800, 538), [], ""),
            overwrite=False,
        )
        assert poster.read_bytes() == "用户自己换的图".encode("utf-8")


# ----------------------------------------------------------------------
class Test人工指定番号:
    """文件名认不出番号时的兜底：手动指定，重新刮削。

    欧美片、改过名的文件、番号只写在种子名里的，都靠这条路。
    """

    def _run(self, tmp_path, files, code="", as_dir=False):
        from unittest.mock import patch

        from app.services import scrape as service

        source = tmp_path / "dl"
        source.mkdir()
        library = tmp_path / "media"
        library.mkdir()
        for name in files:
            (source / name).write_bytes(b"x" * 2048)

        class _Settings:
            medialink_scrape_dir = str(library)
            medialink_library_path = str(library)
            scrape_dir_template = "{number}"
            scrape_file_template = "{number}"
            scrape_category = "日本AV"
            scrape_overwrite = True
            scrape_still_limit = 0
            proxy = ""

        def _meta(target, fetch=True):
            return {"code": target, "title": "T", "casts": "A"}

        with patch.object(service, "get_settings", lambda: _Settings()), \
                patch.object(service, "_load_meta", _meta), \
                patch.object(service, "_cover_bytes", lambda m, c: b""), \
                patch.object(service, "_download", lambda u, c: []), \
                patch.object(service, "_register", lambda *a, **k: None):
            if as_dir:
                results = service.scrape_dir(source, code=code)
            else:
                results = [service.scrape_file(source / files[0], code=code)]

        produced = sorted(p.name for p in library.rglob("*.mp4"))
        return results, produced

    def test_欧美片指定番号后能刮(self, tmp_path):
        results, produced = self._run(
            tmp_path, ["VRLatina_Samy_Pretty_4096p_LR_180.mp4"], code="SSIS-999",
        )
        assert results[0].ok
        assert results[0].code == "SSIS-999"
        assert produced == ["SSIS-999.mp4"]

    def test_认不出的文件名指定番号后能刮(self, tmp_path):
        results, produced = self._run(tmp_path, ["video.mp4"], code="ABS-001")
        assert results[0].ok
        assert produced == ["ABS-001.mp4"]

    def test_指定番号仍保留文件名里的分集(self, tmp_path):
        """指定的是「哪部片」，不是「哪一集」。"""
        _, produced = self._run(tmp_path, ["video-CD2.mp4"], code="ABS-001")
        assert produced == ["ABS-001-CD2.mp4"]

    def test_不指定时报错并提示(self, tmp_path):
        results, _ = self._run(tmp_path, ["video.mp4"])
        assert not results[0].ok
        assert "指定番号" in results[0].error

    def test_非法番号被拒(self, tmp_path):
        results, _ = self._run(tmp_path, ["video.mp4"], code="???")
        assert not results[0].ok
        assert "不是合法格式" in results[0].error

    def test_指定番号覆盖文件名识别结果(self, tmp_path):
        _, produced = self._run(tmp_path, ["ABS-001.mp4"], code="SSIS-999")
        assert produced == ["SSIS-999.mp4"]

    def test_目录只认领认不出的(self, tmp_path):
        """指定一次不能把整个下载目录刮成同一部片。"""
        results, produced = self._run(
            tmp_path, ["ABS-001.mp4", "video.mp4"], code="SSIS-999", as_dir=True,
        )
        assert {r.code for r in results} == {"ABS-001", "SSIS-999"}
        assert produced == ["ABS-001.mp4", "SSIS-999.mp4"]

    def test_认领多个文件自动编分集(self, tmp_path):
        """认不出番号的文件多半也没有 CD 标记，产物名会全撞在一起。
        指定一个番号认领多个文件，意思就是它们是同一部片的多个分集。"""
        _, produced = self._run(
            tmp_path, ["video.mp4", "other.mp4"], code="SSIS-999", as_dir=True,
        )
        assert produced == ["SSIS-999-CD1.mp4", "SSIS-999-CD2.mp4"]

    def test_认领单个文件不编分集(self, tmp_path):
        _, produced = self._run(
            tmp_path, ["video.mp4"], code="SSIS-999", as_dir=True,
        )
        assert produced == ["SSIS-999.mp4"]

    def test_认领时沿用已有分集标记(self, tmp_path):
        _, produced = self._run(
            tmp_path, ["a-CD2.mp4", "b-CD1.mp4"], code="SSIS-999", as_dir=True,
        )
        assert produced == ["SSIS-999-CD1.mp4", "SSIS-999-CD2.mp4"]


# ----------------------------------------------------------------------
class Test指定硬链接目录:
    """本次刮削把产物放到哪，可以临时指定，不必改全局配置。

    与监控目录规则的 target_dir 同一个意思：填了就用它，留空回退。
    """

    def _target(self, tmp_path, target_dir="", dir_template="{category}"):
        from unittest.mock import patch

        from app.services import scrape as service
        from app.utils.mediafile import parse

        class _Settings:
            medialink_scrape_dir = str(tmp_path / "global")
            medialink_library_path = str(tmp_path / "global")
            scrape_dir_template = dir_template
            scrape_file_template = "{number}"
            scrape_category = "日本AV"

        with patch.object(service, "get_settings", lambda: _Settings()):
            info = parse(Path("/downloads/ABS-001.mp4"))
            return service._target_path({"casts": "A"}, info, target_dir)

    def test_留空时用全局配置(self, tmp_path):
        got = self._target(tmp_path)
        assert got.as_posix().startswith((tmp_path / "global").as_posix())

    def test_指定时覆盖全局配置(self, tmp_path):
        custom = tmp_path / "custom"
        got = self._target(tmp_path, target_dir=str(custom))
        assert got.as_posix().startswith(custom.as_posix())
        # 目录模板仍在指定的根目录之下展开
        assert got.as_posix().endswith("日本AV/ABS-001.mp4")

    def test_指定目录配合平铺模板(self, tmp_path):
        custom = tmp_path / "custom"
        got = self._target(tmp_path, target_dir=str(custom), dir_template="")
        assert got == custom / "ABS-001.mp4"


# ----------------------------------------------------------------------
class Test试算产物清单:
    """试算要列全会写出哪些文件，而不只是产物路径。

    刮削真正往媒体库里放的是硬链接 + NFO + 4~13 张图，只显示一个
    mp4 路径，用户无从判断刮完目录会变成什么样。
    """

    def test_列出全部产物(self, tmp_path):
        got = scrape_images.planned_names(tmp_path / "ABS-001.mp4", still_count=3)
        assert got == [
            ("poster", "ABS-001-poster.jpg"),
            ("fanart", "ABS-001-fanart.jpg"),
            ("thumb", "ABS-001-thumb.jpg"),
            ("still", "extrafanart/1.jpg"),
            ("still", "extrafanart/2.jpg"),
            ("still", "extrafanart/3.jpg"),
        ]

    def test_不下剧照时不列剧照(self, tmp_path):
        got = scrape_images.planned_names(tmp_path / "ABS-001.mp4", still_count=0)
        assert all(kind != "still" for kind, _ in got)

    def test_剧照数受上限约束(self, tmp_path):
        got = scrape_images.planned_names(
            tmp_path / "ABS-001.mp4", still_count=scrape_images.MAX_STILLS + 5,
        )
        stills = [n for k, n in got if k == "still"]
        assert len(stills) == scrape_images.MAX_STILLS

    def test_分集各自一套(self, tmp_path):
        cd1 = scrape_images.planned_names(tmp_path / "ABS-001-CD1.mp4")
        cd2 = scrape_images.planned_names(tmp_path / "ABS-001-CD2.mp4")
        assert cd1[0][1] == "ABS-001-CD1-poster.jpg"
        assert cd2[0][1] == "ABS-001-CD2-poster.jpg"

    def test_预告的清单与实际写入一致(self, tmp_path):
        """试算与真写必须用同一套命名，否则试算给的路径是假的。"""
        video = tmp_path / "ABS-001.mp4"
        video.write_bytes(b"x")
        written = scrape_images.write_images(
            video, scrape_images.build_image_set(_encode(800, 538), [], "RIGHT"),
        )
        planned = dict(scrape_images.planned_names(video))
        for kind in ("poster", "fanart", "thumb"):
            assert written[kind] == planned[kind]


class Test试算内容预览:
    """试算要能看到 NFO 的实际内容与图片，不只是文件名。

    元数据抓得对不对，看文件名看不出来 —— 得看内容。
    """

    def _meta(self):
        return {
            "code": "SSIS-001", "title": "日文原标题", "cn_title": "中文标题",
            "outline": "官方简介。", "release_date": "2021-02-18",
            "genres": "美少女,戲劇", "casts": "葵つかさ",
            "banner": "https://pics.dmm.co.jp/x-pl.jpg",
            "still_photo": "https://pics.dmm.co.jp/1.jpg,https://pics.dmm.co.jp/2.jpg",
            "local_banner": "",
        }

    def test_预览与实际写入用同一份数据(self):
        """两边各造一次 NfoData 迟早会对不上，那时预览就是假的。"""
        from app.services.scrape import build_nfo_data
        from app.utils.mediafile import parse

        info = parse(Path("SSIS-001-CD2.mp4"))
        data = build_nfo_data("SSIS-001", self._meta(), info, total_parts=2)
        root = ElementTree.fromstring(nfo.render(data))
        assert root.findtext("title") == "SSIS-001 中文标题 CD2"
        assert root.findtext("plot") == "官方简介。"
        # 类别照样繁转简
        assert "戏剧" in [e.text for e in root.findall("genre")]

    def test_图片走代理而非源站直连(self):
        """图源有防盗链，浏览器直连拿到的是 403。"""
        from app.api.endpoints.scrape import _preview_images

        class _S:
            scrape_still_limit = 2

        got = _preview_images(self._meta(), _S())
        assert got["cover"].startswith("/api/v1/image-")
        assert len(got["stills"]) == 2
        assert all(u.startswith("/api/v1/image-") for u in got["stills"])

    def test_海报给裁好的背景给原图(self):
        """源站封面多是横版双拼，显示原图看不出 Emby 里最终长什么样。
        海报要裁、背景不裁 —— 刮削时就是这么落盘的。"""
        from app.api.endpoints.scrape import _preview_images

        class _S:
            scrape_still_limit = 0

        got = _preview_images(self._meta(), _S())
        assert got["poster"].endswith("poster=1")
        assert "poster=1" not in got["fanart"]

    def test_本地缓存也走裁切(self):
        from app.api.endpoints.scrape import _preview_images

        class _S:
            scrape_still_limit = 0

        meta = dict(self._meta(), banner="", local_banner="SSIS-001/banner.jpg")
        got = _preview_images(meta, _S())
        assert got["poster"].startswith("/api/v1/image-local")
        assert got["poster"].endswith("poster=1")
        assert "poster=1" not in got["fanart"]

    def test_裁切与刮削产物同一套逻辑(self):
        """预览裁出来的必须和真正落盘的是同一张，否则预览没有意义。"""
        from app.api.endpoints.picproxy import _as_poster

        source = _encode(800, 538, split=True)
        assert _as_poster(source, "RIGHT") == scrape_images.crop_poster(
            source, "RIGHT",
        )

    def test_剧照数受配置约束(self):
        from app.api.endpoints.scrape import _preview_images

        class _S:
            scrape_still_limit = 1

        assert len(_preview_images(self._meta(), _S())["stills"]) == 1

    def test_没有封面时不给地址(self):
        from app.api.endpoints.scrape import _preview_images

        class _S:
            scrape_still_limit = 3

        got = _preview_images({"code": "X-1"}, _S())
        assert got["cover"] == ""
        assert got["stills"] == []


class Test图源白名单:
    def test_放行真实图源(self):
        """missav 的图在 fourhoi.com 上，而它常是唯一给中文标题的源 ——
        不放行就只能显示成裂图。"""
        from app.api.endpoints.picproxy import _is_allowed

        for url in (
            "https://pics.dmm.co.jp/digital/video/x/xpl.jpg",
            "https://fourhoi.com/abp-554/cover-n.jpg",
            "https://cdn.avbase.net/x.jpg",
        ):
            assert _is_allowed(url), url

    def test_挡住仿冒域名(self):
        from app.api.endpoints.picproxy import _is_allowed

        assert not _is_allowed("https://fourhoi.com.evil.com/x.jpg")
        assert not _is_allowed("https://evil.com/x.jpg")


class Test产物路径警告:
    """输出目录已经以分类名结尾、模板里又写一次 {category} 时，
    会得到 日本AV/日本AV/… —— 配置上很自然就会踩到，刮完才发现
    就得手工挪文件。"""

    def _warn(self, path):
        from app.api.endpoints.scrape import _warn_paths

        return _warn_paths([{"target": path}])

    def test_重复目录名要警告(self):
        got = self._warn("/volume3/h_video/日本AV/日本AV/推川ゆうり/JMTY-083.mp4")
        assert got and "日本AV" in got[0]

    def test_正常路径不警告(self):
        assert self._warn("/volume3/h_video/日本AV/推川ゆうり/JMTY-083.mp4") == []


# ----------------------------------------------------------------------
class Test路径诊断:
    """路径不存在时要说清断在哪一层。

    宿主机上看得见、容器里没有，最常见的原因是那一层没挂进容器 ——
    只说「路径不存在」会让人以为是路径写错，对着正确的路径反复试。
    """

    def _explain(self, target):
        from app.api.endpoints.scrape import _explain_missing

        return _explain_missing(Path(target))

    def test_整层没挂时提示挂载(self, tmp_path):
        got = self._explain(tmp_path / "volume3" / "h_video" / "x.mp4")
        assert "没挂进容器" in got
        assert "up -d" in got

    def test_只有文件名不对时不提挂载(self, tmp_path):
        """父目录存在说明挂载没问题，再提挂载就是误导。"""
        got = self._explain(tmp_path / "missing.mp4")
        assert "没挂进容器" not in got
        assert "missing.mp4" in got

    def test_列出断点目录的内容(self, tmp_path):
        (tmp_path / "Download").mkdir()
        got = self._explain(tmp_path / "Downloads" / "x.mp4")
        # 名字打错时，列出实际有什么就能一眼看出
        assert "Download" in got


# ----------------------------------------------------------------------
class Test多源合并:
    def test_逐字段挑最全的(self):
        """first-wins 会让稀疏的那个站决定最终质量。
        刮削的产物长期躺在媒体库里，缺字段就是永久缺。"""
        merged = merge.merge_details([
            merge.SourceResult("javdb", {
                "title": "Short", "release_date": "2024-03-15",
                "casts": "深田えいみ", "banner": "http://javdb/b.jpg",
            }),
            merge.SourceResult("javbus", {
                "title": "A Much Longer Complete Title",
                "casts": "深田えいみ,三上悠亜",
                "genres": "単体作品,巨乳,中出し",
            }),
            merge.SourceResult("javlibrary", {"star": 8.5, "series": "テスト系列"}),
        ], "ABS-001")

        assert merged["title"] == "A Much Longer Complete Title"
        assert merged["casts"] == "深田えいみ,三上悠亜"
        assert merged["star"] == 8.5
        assert merged["series"] == "テスト系列"
        # first 策略：先到的站给了就不换
        assert merged["banner"] == "http://javdb/b.jpg"
        assert merged["release_date"] == "2024-03-15"

    @pytest.mark.parametrize(
        "text,expect",
        [
            ("女友不在的三天 原本快禁欲一个月", True),
            ("绝对铁板情况1 铃村爱理", True),
            # 有假名就是日文，哪怕汉字占多数
            ("一ヶ月間の禁欲の果てに彼女の", False),
            ("葵つかさ", False),
            ("铃村爱里 (鈴村あいり)", False),
            ("ABS-001", False),
            ("", False),
        ],
    )
    def test_中日文判定(self, text, expect):
        """中日共用汉字，靠汉字分不出语言，假名才是日文独有的标志。"""
        assert merge._has_chinese(text) is expect

    def test_标题优先中文而非最长(self):
        """实测 SSIS-001：日文标题 55 字、中文 40 字。

        纯比长度会让日文永远赢 —— 中文媒体库里显示的却全是日文，
        与「优先中文」的预期正好相反。
        """
        merged = merge.merge_details([
            merge.SourceResult("avbase", {
                "title": "一ヶ月間の禁欲の果てに彼女のルームメイト2人と"
                         "浮気SEXだけに没頭した彼女不在の3日間。 葵つかさ 乙白さやか",
            }),
            merge.SourceResult("missav", {
                "title": "女友不在的三天 原本快禁欲一个月 却沉醉于和女友的同学出轨性爱",
            }),
        ])
        assert merged["title"].startswith("女友不在的三天")

    def test_同为中文时取最长(self):
        merged = merge.merge_details([
            merge.SourceResult("a", {"title": "短标题"}),
            merge.SourceResult("b", {"title": "长一些的完整中文标题"}),
        ])
        assert merged["title"] == "长一些的完整中文标题"

    def test_只有日文时仍然采用(self):
        """没有中文源时不能因为「不是中文」就把标题丢掉。"""
        merged = merge.merge_details([
            merge.SourceResult("avbase", {"title": "葵つかさの作品"}),
        ])
        assert merged["title"] == "葵つかさの作品"

    def test_列表字段按元素个数比(self):
        """一个长名字的演员会让单人的那份比双人的字符串更长，
        所以列表字段必须按元素个数比。"""
        merged = merge.merge_details([
            merge.SourceResult("a", {"casts": "非常長い名前の女優さんです"}),
            merge.SourceResult("b", {"casts": "A,B"}),
        ])
        assert merged["casts"] == "A,B"

    def test_跳过失败的源(self):
        merged = merge.merge_details([
            merge.SourceResult("failed", None, 0.0, "timeout"),
            merge.SourceResult("ok", {"title": "T"}),
        ])
        assert merged == {"title": "T"}

    def test_全部失败返回空(self):
        assert merge.merge_details([merge.SourceResult("x", None)]) == {}

    def test_挑分辨率最高的封面(self):
        """同一张封面各站给的尺寸差别很大。海报要裁一半还要放大显示，
        清晰度直接决定观感。不能按体积比 —— 小尺寸高质量的 JPEG
        会比大尺寸低质量的更大。"""
        candidates = [
            ("缩略图", _encode(300, 200, quality=95)),
            ("原图", _encode(800, 538, quality=60)),
            ("中图", _encode(500, 336, quality=90)),
        ]
        name, data = merge.pick_best_image(candidates)
        assert name == "原图"
        with Image.open(io.BytesIO(data)) as image:
            assert image.size == (800, 538)

    def test_全空候选返回空(self):
        assert merge.pick_best_image([("a", b""), ("b", b"")]) == ("", b"")
