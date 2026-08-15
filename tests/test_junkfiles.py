"""广告文件识别。

真实案例（SNOS-183）：种子里 1 个正片 + 2 个引流视频 + 1 个网页 + 1 个文本。
不标记的话会被刮削工具清掉，媒体服务器随即发删除事件，联动删除把整部片
的种子和正片一起删了。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.utils.junkfiles import pick_junk_files

MB = 1024 * 1024
GB = 1024 * MB


class TestPickJunkFiles:
    def test_real_case_snos183(self):
        """用户实际遇到的那个种子。"""
        files = [
            {"path": "/dl/SNOS-183-U/489155.com@SNOS-183-U.mp4", "size": 5 * GB},
            {"path": "/dl/SNOS-183-U/台湾uu美少女直播 20年信誉保证服务全球.mp4", "size": 20 * MB},
            {"path": "/dl/SNOS-183-U/社 區 最 新 情 報.mp4", "size": 15 * MB},
            {"path": "/dl/SNOS-183-U/聚 合 全 網 H 直 播.html", "size": 5 * 1024},
            {"path": "/dl/SNOS-183-U/最 新 位 址 獲 取：489155.com 收藏不迷路.txt", "size": 200},
        ]
        junk = pick_junk_files(files)

        assert "/dl/SNOS-183-U/489155.com@SNOS-183-U.mp4" not in junk, "正片不能被标记"
        assert len(junk) == 4
        assert all("489155.com@SNOS-183-U.mp4" not in p for p in junk)

    def test_single_file_torrent_untouched(self):
        """单文件种子没有广告可挑。"""
        assert pick_junk_files([{"path": "/dl/a.mp4", "size": 5 * GB}]) == []

    def test_multi_part_movie_kept(self):
        """分集种子各集大小接近，不能把某一集当广告。"""
        files = [
            {"path": "/dl/x/CD1.mp4", "size": 2 * GB},
            {"path": "/dl/x/CD2.mp4", "size": 2 * GB},
            {"path": "/dl/x/CD3.mp4", "size": 1900 * MB},
        ]
        assert pick_junk_files(files) == []

    def test_large_non_video_kept(self):
        """大的非影片文件可能是字幕包或花絮，留着。"""
        files = [
            {"path": "/dl/x/movie.mp4", "size": 5 * GB},
            {"path": "/dl/x/extras.zip", "size": 200 * MB},
            {"path": "/dl/x/ad.html", "size": 3 * 1024},
        ]
        junk = pick_junk_files(files)
        assert junk == ["/dl/x/ad.html"]

    def test_small_movie_with_small_ads(self):
        """正片本身不大时，按比例算出来的阈值要有绝对下限兜底。"""
        files = [
            {"path": "/dl/x/movie.mp4", "size": 800 * MB},
            {"path": "/dl/x/ad.mp4", "size": 10 * MB},
        ]
        assert pick_junk_files(files) == ["/dl/x/ad.mp4"]

    def test_no_video_means_no_judgement(self):
        """一个影片都没有时无从判断，全留着。"""
        files = [
            {"path": "/dl/x/a.zip", "size": 100 * MB},
            {"path": "/dl/x/b.txt", "size": 100},
        ]
        assert pick_junk_files(files) == []

    def test_never_marks_everything(self):
        """兜底：不能把所有文件都标成垃圾。"""
        files = [
            {"path": "/dl/x/a.mp4", "size": 0},
            {"path": "/dl/x/b.mp4", "size": 0},
        ]
        assert pick_junk_files(files) == []

    def test_empty_and_malformed_input(self):
        assert pick_junk_files([]) == []
        assert pick_junk_files([{"path": "", "size": 1}, {"nope": 1}]) == []

    def test_unknown_suffix_video_kept(self):
        """扩展名不认识的大文件不当广告 —— 判断不了就别动。"""
        files = [
            {"path": "/dl/x/movie.mp4", "size": 5 * GB},
            {"path": "/dl/x/weird.xyz", "size": 4 * GB},
        ]
        # .xyz 不在影片列表里，但它有 4GB，超过非影片保留阈值
        assert pick_junk_files(files) == []
