"""Bounded image header inspection without loading image pixels in the API.

PNG/GIF layouts follow W3C specifications; WebP follows its RIFF container
specification, JPEG follows SOF marker layout. Actual pixel decoding remains
inside the resource-limited training sandbox. This is not a decoder.
"""
import struct
import zlib

from app.services.lab_archive import LabDataError

MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_EDGE = 16_384


def _dimensions(width, height):
    if not 0 < width <= MAX_IMAGE_EDGE or not 0 < height <= MAX_IMAGE_EDGE or width * height > MAX_IMAGE_PIXELS:
        raise LabDataError("图像尺寸无效或像素数超过限制（单边 16384、总计 4000 万像素）")
    return width, height


def _read(stream, count):
    body = stream.read(count)
    if len(body) != count:
        raise LabDataError("图像头部或文件内容不完整")
    return body


def inspect_image(path):
    size = path.stat().st_size
    suffix = path.suffix.lower()
    with path.open("rb") as stream:
        if suffix == ".png":
            header = _read(stream, 33)
            if (header[:8] != b"\x89PNG\r\n\x1a\n" or header[8:16] != b"\0\0\0\rIHDR"
                    or zlib.crc32(header[12:29]) != int.from_bytes(header[29:33], "big")):
                raise LabDataError("PNG 图像头部无效")
            width, height = struct.unpack(">II", header[16:24])
            depth, color, compression, filtering, interlace = header[24:29]
            allowed_depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
            if depth not in allowed_depths.get(color, set()) or compression or filtering or interlace not in {0, 1}:
                raise LabDataError("PNG 图像编码参数无效")
            _dimensions(width, height)
            if size < 57:
                raise LabDataError("PNG 图像内容不完整")
            stream.seek(-12, 2)
            if _read(stream, 12) != b"\0\0\0\0IEND\xaeB`\x82":
                raise LabDataError("PNG 图像缺少完整结束标记")
            return width, height
        if suffix == ".gif":
            header = _read(stream, 13)
            if header[:6] not in {b"GIF87a", b"GIF89a"}:
                raise LabDataError("GIF 图像头部无效")
            width, height = struct.unpack("<HH", header[6:10])
            _dimensions(width, height)
            stream.seek(-1, 2)
            if _read(stream, 1) != b";" or size < 20:
                raise LabDataError("GIF 图像内容不完整")
            return width, height
        if suffix in {".jpg", ".jpeg"}:
            if _read(stream, 2) != b"\xff\xd8":
                raise LabDataError("JPEG 图像头部无效")
            dimensions = None
            sof = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
            for _ in range(4096):
                if _read(stream, 1) != b"\xff":
                    raise LabDataError("JPEG 标记结构无效")
                marker = _read(stream, 1)[0]
                while marker == 0xFF:
                    marker = _read(stream, 1)[0]
                if marker in {0xDA, 0xD9}:
                    break
                if marker in {0x01, *range(0xD0, 0xD8)}:
                    continue
                length = int.from_bytes(_read(stream, 2), "big")
                if length < 2 or stream.tell() + length - 2 > size:
                    raise LabDataError("JPEG 分段长度无效")
                if marker in sof:
                    if length < 8:
                        raise LabDataError("JPEG 尺寸段无效")
                    payload = _read(stream, 6)
                    dimensions = _dimensions(int.from_bytes(payload[3:5], "big"), int.from_bytes(payload[1:3], "big"))
                    stream.seek(length - 8, 1)
                else:
                    stream.seek(length - 2, 1)
            if dimensions is None:
                raise LabDataError("JPEG 缺少有效尺寸信息")
            stream.seek(-2, 2)
            if _read(stream, 2) != b"\xff\xd9":
                raise LabDataError("JPEG 图像内容不完整")
            return dimensions
        if suffix == ".webp":
            header = _read(stream, 12)
            if header[:4] != b"RIFF" or header[8:] != b"WEBP" or int.from_bytes(header[4:8], "little") + 8 != size:
                raise LabDataError("WebP 容器头部无效")
            dimensions = None
            while stream.tell() < size:
                kind = _read(stream, 4)
                length = int.from_bytes(_read(stream, 4), "little")
                end = stream.tell() + length + (length % 2)
                if end > size:
                    raise LabDataError("WebP 分段长度无效")
                payload = _read(stream, min(10, length))
                current = None
                if kind == b"VP8X" and length == 10:
                    current = _dimensions(int.from_bytes(payload[4:7], "little") + 1, int.from_bytes(payload[7:10], "little") + 1)
                elif kind == b"VP8 " and len(payload) >= 10 and payload[3:6] == b"\x9d\x01\x2a":
                    current = _dimensions(int.from_bytes(payload[6:8], "little") & 0x3FFF, int.from_bytes(payload[8:10], "little") & 0x3FFF)
                elif kind == b"VP8L" and len(payload) >= 5 and payload[0] == 0x2F:
                    bits = int.from_bytes(payload[1:5], "little")
                    current = _dimensions((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
                if current:
                    dimensions = dimensions or current
                stream.seek(end)
            if dimensions is None:
                raise LabDataError("WebP 缺少有效尺寸信息")
            return dimensions
    raise LabDataError("图像格式不受支持")
