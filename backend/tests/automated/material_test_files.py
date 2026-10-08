"""Small valid containers for tests; no external PDF/Office dependency."""
from io import BytesIO
import zipfile


def minimal_pdf(label='material'):
    output = bytearray(f'%PDF-1.4\n% {label}\n'.encode())
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>',
               b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
               b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << >> >>']
    offsets = [0]
    for index, body in enumerate(objects, 1):
        offsets.append(len(output))
        output.extend(f'{index} 0 obj\n'.encode() + body + b'\nendobj\n')
    xref = len(output)
    output.extend(b'xref\n0 4\n0000000000 65535 f \n')
    for offset in offsets[1:]:
        output.extend(f'{offset:010d} 00000 n \n'.encode())
    output.extend(f'trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return bytes(output)


def zip_bytes(members, *, compression=zipfile.ZIP_DEFLATED):
    result = BytesIO()
    with zipfile.ZipFile(result, 'w', compression=compression) as archive:
        for name, contents in members:
            archive.writestr(name, contents)
    return result.getvalue()


def minimal_pdf_xref_stream():
    output = bytearray(b'%PDF-1.7\n')
    offsets = [0]
    for number, body in enumerate([b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << >> >>'], 1):
        offsets.append(len(output))
        output.extend(f'{number} 0 obj\n'.encode() + body + b'\nendobj\n')
    xref = len(output); offsets.append(xref)
    data = b'\x00' + b'\x00' * 4 + b'\xff\xff'
    data += b''.join(b'\x01' + position.to_bytes(4, 'big') + b'\x00\x00' for position in offsets[1:])
    output.extend(f'4 0 obj\n<< /Type /XRef /Size 5 /Root 1 0 R /W [1 4 2] /Length {len(data)} >>\nstream\n'.encode())
    output.extend(data + b'\nendstream\nendobj\n')
    output.extend(f'startxref\n{xref}\n%%EOF\n'.encode())
    return bytes(output)


def minimal_pptx():
    return zip_bytes([
        ('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>'),
        ('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>'),
        ('ppt/presentation.xml', '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"/>'),
    ])
