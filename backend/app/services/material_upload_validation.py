"""Bounded container checks, without extracting archives or executing content.

These checks establish basic structure; they are not antivirus or content disarm.
"""
import re
import stat
import struct
import unicodedata
import zipfile

from app.errors import api_error

MAX_ARCHIVE_MEMBERS = 5000
MAX_DIRECTORY_BYTES = 8 * 1024 * 1024
MAX_EXPANDED_BYTES = 2 * 1024 * 1024 * 1024
MAX_MEMBER_BYTES = 512 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
MAX_PDF_XREF_BYTES = 2 * 1024 * 1024


def invalid(message):
    raise api_error(415, 'MATERIAL_CONTENT_INVALID', message)


def file_size(stream):
    stream.seek(0, 2)
    length = stream.tell()
    stream.seek(0)
    return length


def _archive_directory(stream, length):
    # Bound the directory before ZipFile allocates/decodes its member table.
    window = min(length, 65557)
    stream.seek(length - window)
    tail = stream.read(window)
    index = tail.rfind(b'PK\x05\x06')
    while index >= 0:
        if index + 22 <= len(tail):
            values = struct.unpack('<4s4H2IH', tail[index:index + 22])
            if index + 22 + values[7] == len(tail):
                break
        index = tail.rfind(b'PK\x05\x06', 0, index)
    if index < 0:
        invalid('ZIP 目录或结束记录无效')
    _, disk, directory_disk, disk_count, count, directory_bytes, offset, _ = values
    if (disk or directory_disk or disk_count != count or not 0 < count <= MAX_ARCHIVE_MEMBERS
            or not 0 < directory_bytes <= MAX_DIRECTORY_BYTES
            or offset + directory_bytes != length - window + index):
        invalid('ZIP 分卷、ZIP64、目录大小或成员数量不符合限制')
    stream.seek(0)
    return count, offset


def validate_archive(stream, length, *, presentation=False):
    count, directory_offset = _archive_directory(stream, length)
    seen, files, exact_files, total, intervals = set(), set(), set(), 0, []
    try:
        with zipfile.ZipFile(stream) as archive:
            members = archive.infolist()
            if len(members) != count:
                invalid('ZIP 成员数量与目录不一致')
            for member in members:
                name = unicodedata.normalize('NFKC', member.orig_filename)
                parts = name.rstrip('/').split('/')
                if (not name or len(name) > 1024 or name.startswith('/') or '\\' in name or ':' in name
                        or any(ord(c) < 32 or ord(c) == 127 for c in name)
                        or any(p in ('', '.', '..') or p.endswith(('.', ' ')) for p in parts)):
                    invalid('ZIP 成员路径无效')
                normalized = '/'.join(parts).casefold()
                if normalized in seen:
                    invalid('ZIP 含重复或冲突的成员路径')
                seen.add(normalized)
                if not member.is_dir():
                    files.add(normalized)
                    exact_files.add(member.orig_filename)
                mode = stat.S_IFMT(member.external_attr >> 16)
                if mode not in (0, stat.S_IFREG, stat.S_IFDIR) or member.flag_bits & 0x41:
                    invalid('ZIP 不支持链接、特殊文件或加密成员')
                if member.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED,
                                                zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA):
                    invalid('ZIP 压缩方法不支持')
                total += member.file_size
                if (member.file_size > MAX_MEMBER_BYTES or total > MAX_EXPANDED_BYTES
                        or member.file_size > max(member.compress_size, 1) * MAX_COMPRESSION_RATIO):
                    invalid('ZIP 展开大小或压缩比超过限制')
                if not 0 <= member.header_offset <= directory_offset - 30:
                    invalid('ZIP 成员偏移无效')
                stream.seek(member.header_offset)
                header = stream.read(30)
                if len(header) != 30:
                    invalid('ZIP 成员头被截断')
                local = struct.unpack('<4s5H3I2H', header)
                if local[0] != b'PK\x03\x04' or local[2] != member.flag_bits or local[3] != member.compress_type:
                    invalid('ZIP 本地成员头与目录不一致')
                if local[9] > 4096 or local[10] > 65535:
                    invalid('ZIP 成员头超出限制')
                local_name = stream.read(local[9]).decode('utf-8' if local[2] & 0x800 else 'cp437')
                if local_name != member.orig_filename:
                    invalid('ZIP 成员名称与目录不一致')
                if not local[2] & 8 and (local[6], local[7], local[8]) != (member.CRC, member.compress_size, member.file_size):
                    invalid('ZIP 成员大小与目录不一致')
                end = member.header_offset + 30 + local[9] + local[10] + member.compress_size
                if end > directory_offset:
                    invalid('ZIP 成员数据被截断')
                intervals.append((member.header_offset, end))
            ordered = sorted(intervals)
            if any(previous[1] > current[0] for previous, current in zip(ordered, ordered[1:])):
                invalid('ZIP 成员数据相互重叠')
            if any('/'.join(name.split('/')[:i]) in files for name in seen for i in range(1, len(name.split('/')))):
                invalid('ZIP 文件与目录路径冲突')
            if presentation:
                required = {'[Content_Types].xml', '_rels/.rels', 'ppt/presentation.xml'}
                if not required <= exact_files or any(name.endswith('/vbaproject.bin') for name in files):
                    invalid('PPTX 缺少必要成员或包含不匹配的宏内容')
    except (zipfile.BadZipFile, UnicodeDecodeError, ValueError, struct.error, NotImplementedError) as exc:
        raise api_error(415, 'MATERIAL_CONTENT_INVALID', 'ZIP/PPTX 结构无效') from exc
    finally:
        stream.seek(0)


def validate_pdf(stream, length):
    stream.seek(0)
    if not re.match(rb'%PDF-(?:1\.[0-7]|2\.0)(?:\s|%)', stream.read(32)):
        invalid('PDF 文件头无效')
    stream.seek(max(0, length - 4096))
    ending = re.search(rb'startxref\s+([0-9]{1,20})\s+%%EOF\s*\Z', stream.read(4096))
    if not ending:
        invalid('PDF 缺少有效结束标记或交叉引用位置')
    offset = int(ending[1])
    if not 0 < offset < length:
        invalid('PDF 交叉引用偏移无效')
    stream.seek(offset)
    block = stream.read(min(length - offset, MAX_PDF_XREF_BYTES))
    object_offsets = {}
    if block.startswith(b'xref'):
        trailer_at = block.find(b'trailer')
        if trailer_at < 0:
            invalid('PDF 交叉引用或尾字典超出结构检查限制')
        lines = block[4:trailer_at].strip().splitlines()
        cursor, entries = 0, 0
        while cursor < len(lines):
            subsection = re.fullmatch(rb'([0-9]{1,10})\s+([0-9]{1,10})', lines[cursor].strip())
            if not subsection:
                invalid('PDF 交叉引用分段无效')
            count = int(subsection[2]); entries += count; cursor += 1
            if not 0 < count <= 100000 or entries > 100000 or cursor + count > len(lines):
                invalid('PDF 交叉引用数量无效或超过限制')
            start = int(subsection[1])
            for number, line in enumerate(lines[cursor:cursor + count], start):
                entry = re.fullmatch(rb'([0-9]{10})\s+([0-9]{5})\s+([nf])\s*', line)
                if not entry or (entry[3] == b'n' and not 0 < int(entry[1]) < offset):
                    invalid('PDF 交叉引用条目无效')
                if entry[3] == b'n':
                    object_offsets[number] = (int(entry[1]), int(entry[2]))
            cursor += count
        dictionary = block[trailer_at + 7:]
    else:
        # PDF 1.5+ cross-reference streams: inspect the bounded dictionary only,
        # without inflating stream data or interpreting page/object content.
        stream_marker = re.search(rb'\bstream(?:\r\n|\n|\r)', block)
        if not stream_marker:
            invalid('PDF 交叉引用流无效')
        dictionary = block[:stream_marker.start()]
        widths = re.search(rb'/W\s*\[\s*([0-8])\s+([0-8])\s+([0-8])\s*\]', dictionary)
        stream_length = re.search(rb'/Length\s+([1-9][0-9]{0,6})\b(?!\s+[0-9]+\s+R)', dictionary)
        if (not re.match(rb'[0-9]+\s+[0-9]+\s+obj\s*<<', dictionary)
                or not re.search(rb'/Type\s*/XRef\b', dictionary) or not widths or not stream_length
                or not sum(int(value) for value in widths.groups())):
            invalid('PDF 交叉引用流无效')
        data_length = int(stream_length[1])
        end = stream_marker.end() + data_length
        if data_length > MAX_PDF_XREF_BYTES or not block[end:end + 12].lstrip(b'\r\n').startswith(b'endstream'):
            invalid('PDF 交叉引用流长度无效或超出检查限制')
        if b'/Filter' not in dictionary:
            size_match = re.search(rb'/Size\s+([1-9][0-9]{0,5})\b', dictionary)
            index_match = re.search(rb'/Index\s*\[([^\]]{1,4096})\]', dictionary)
            if index_match:
                if not re.fullmatch(rb'[0-9\s]+', index_match[1]):
                    invalid('PDF 交叉引用索引无效')
                indexes = [int(value) for value in index_match[1].split()]
                if len(indexes) % 2 or not size_match or any(
                        count <= 0 or start + count > int(size_match[1]) for start, count in zip(indexes[::2], indexes[1::2])):
                    invalid('PDF 交叉引用索引无效')
                row_count = sum(indexes[1::2])
            else:
                row_count = int(size_match[1]) if size_match else 0
            if not 0 < row_count <= 100000 or data_length != row_count * sum(int(value) for value in widths.groups()):
                invalid('PDF 交叉引用流条目数量与长度不一致')
    size = re.search(rb'/Size\s+([1-9][0-9]{0,5})\b', dictionary)
    root = re.search(rb'/Root\s+([1-9][0-9]{0,5})\s+([0-9]{1,5})\s+R\b', dictionary)
    if not size or int(size[1]) > 100001 or not root or int(root[1]) >= int(size[1]) or int(root[2]) > 65535:
        invalid('PDF 缺少基本文档目录或对象数量')
    stream.seek(0)
    prefix = stream.read(min(length, 1024 * 1024))
    first_object = re.search(rb'\b[0-9]+\s+[0-9]+\s+obj\b', prefix)
    if not first_object or prefix.find(b'endobj', first_object.end()) < 0:
        invalid('PDF 缺少有效基本对象')
    if block.startswith(b'xref'):
        root_number, root_generation = int(root[1]), int(root[2])
        location = object_offsets.get(root_number)
        if location:
            if location[1] != root_generation:
                invalid('PDF 文档目录与交叉引用不一致')
            stream.seek(location[0])
            catalog = stream.read(min(length - location[0], 65536))
        else:
            # Incremental PDFs may keep the catalog in an earlier xref section.
            previous = re.search(rb'/Prev\s+([0-9]{1,20})\b', dictionary)
            marker = re.search(rb'\b' + str(root_number).encode() + rb'\s+' + str(root_generation).encode() + rb'\s+obj\b', prefix)
            if not previous or not 0 < int(previous[1]) < offset or not marker:
                invalid('PDF 文档目录无法通过基本结构检查')
            catalog = prefix[marker.start():marker.start() + 65536]
        expected = str(root_number).encode() + rb'\s+' + str(root_generation).encode() + rb'\s+obj\b'
        if not re.match(expected, catalog) or not re.search(rb'/Type\s*/Catalog\b', catalog.split(b'endobj', 1)[0]):
            invalid('PDF 文档目录对象无效')
    stream.seek(0)


def validate_video_box(stream, length):
    stream.seek(0)
    head = stream.read(16)
    size = int.from_bytes(head[:4], 'big')
    header_bytes = 8
    if size == 1:
        size = int.from_bytes(head[8:16], 'big'); header_bytes = 16
    if head[4:8] != b'ftyp' or not header_bytes + 8 <= size <= min(length, 65536) or (size - header_bytes - 8) % 4:
        invalid('视频 ftyp 容器结构无效')
    stream.seek(0)
