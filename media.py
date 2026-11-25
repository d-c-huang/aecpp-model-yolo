"""Decode local video by stream content and sample its presentation timeline."""
from contextlib import contextmanager
import math
from pathlib import Path


MOVIE_TYPES = [("视频文件", tuple("*." + ext for ext in
    "mp4 mkv mov avi webm m4v mpg mpeg m2v ts mts m2ts vob wmv asf flv f4v 3gp 3g2 ogv rm rmvb mxf h264 264 hevc h265".split())),
    ("所有文件", "*.*")]


class Movie:
    def __init__(self, av, path, progress):
        self.av, self.path, self.progress = av, path, progress
        self.container = None
        self.open()
        try:
            stream = self.stream
            self.duration = float(stream.duration * stream.time_base) if stream.duration else 0
            if self.duration <= 0 and self.container.duration:
                self.duration = self.container.duration / av.time_base
            self.seekable = bool(self.duration > 0)
            if self.duration <= 0 or not math.isfinite(self.duration):
                self.progress("文件没有可靠时长，正在扫描视频轨…")
                last = None
                for i, (last, _) in enumerate(self.decoded(0)):
                    if i % 240 == 0:
                        self.progress(f"正在测量电影时长 · {last:.1f} 秒")
                if last is None:
                    raise ValueError("视频轨没有可解码的画面")
                self.duration = last + self.interval
                self.open()
        except BaseException:
            self.close()
            raise

    def open(self):
        self.close()
        self.container = self.av.open(str(self.path))
        # attached_pic is the FFmpeg disposition bit for an embedded cover image.
        streams = [s for s in self.container.streams.video if not int(s.disposition) & 0x400]
        if not streams:
            self.close()
            raise ValueError("文件里没有可用的视频轨（可能只有音频或封面）")
        self.stream = max(streams, key=lambda s:(bool(int(s.disposition) & 1), s.width*s.height))
        self.interval = 1/float(self.stream.average_rate or self.stream.guessed_rate or 25)
        self.origin = (self.stream.start_time or 0)*self.stream.time_base
        self.stream.thread_type = "AUTO"

    def close(self):
        if self.container is not None:
            self.container.close()
            self.container = None

    def decoded(self, fallback_start):
        t = fallback_start
        for index,frame in enumerate(self.container.decode(self.stream)):
            if frame.pts is not None:
                t = float(frame.pts * frame.time_base - self.origin)
            if index and index % 500 == 0:
                self.progress(f"顺序读取视频 · {max(0,t):.1f} 秒")
            yield max(0.,t),frame
            t += self.interval

    def samples(self, times):
        """Yield (actual seconds, RGB image) for increasing requested times."""
        iterator = self.decoded(0)
        current = None
        for target in times:
            if self.seekable and target - (current[0] if current else 0) > 3:
                try:
                    offset = int((target + self.origin) / self.stream.time_base)
                    self.container.seek(offset, stream=self.stream, backward=True)
                    iterator = self.decoded(target)
                    candidate = next(iterator,None)
                    if candidate is None or candidate[0] > target + self.interval*2:
                        raise ValueError("索引未定位到请求时段")
                    current = candidate
                except (self.av.FFmpegError, ValueError, OverflowError):
                    self.seekable = False
                    self.open()
                    iterator, current = self.decoded(0), None
            while current is None or current[0] + self.interval*.5 < target:
                following = next(iterator,None)
                if following is None:
                    if current is None:
                        raise ValueError("未解码出视频画面，请检查文件是否完整")
                    break
                current = following
            t,frame = current
            image = frame.to_image()
            angle = getattr(frame,"rotation",0)
            if angle:
                image = image.rotate(angle,expand=True)
            yield t,image


@contextmanager
def open_movie(path, progress):
    try:
        import av
    except ImportError as exc:
        raise ValueError("缺少视频解码组件。请在客户端目录运行 python -m pip install -r requirements.txt") from exc
    movie = None
    try:
        movie = Movie(av,Path(path),progress)
        yield movie
    except av.FFmpegError as exc:
        raise ValueError(f"无法解码「{Path(path).name}」：{exc.strerror or exc}") from exc
    finally:
        if movie:
            movie.close()
