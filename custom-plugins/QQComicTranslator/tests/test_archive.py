from __future__ import annotations

import base64
import builtins
import io
import os
import shutil
import stat
import struct
import sys
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from pkg import archive


def picture(fmt="PNG", size=(8, 4), *, exif=None):
    image = Image.new("RGB", size, "white")
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **({"exif": exif} if exif else {}))
    image.close()
    return buffer.getvalue()


def zip_bytes(items):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as output:
        for name, data in items:
            output.writestr(name, data)
    return buffer.getvalue()


def test_formats_natural_order_and_basenames():
    items = [
        ("chapter/page10.webp", picture("WEBP")),
        ("chapter/page2.bmp", picture("BMP")),
        ("chapter/page1.png", picture()),
        ("chapter/page3.jpeg", picture("JPEG")),
        ("notes.md", b"ignored"),
    ]
    with archive.open_archive(zip_bytes(items)) as comic:
        assert [page.basename for page in comic.pages] == [
            "page1.png",
            "page2.bmp",
            "page3.jpeg",
            "page10.webp",
        ]
        assert comic.ignored_entries == 1
        assert comic.pages[0].logical_path == "chapter/page1.png"
        for index in range(4):
            page = comic.prepare_page(index)
            assert (page.width, page.height) == (8, 4)
            assert page.jpeg_bytes.startswith(b"\xff\xd8\xff")
            with Image.open(io.BytesIO(page.jpeg_bytes)) as image:
                assert image.format == "JPEG" and image.size == (8, 4)
        assert not any(isinstance(value, Image.Image) for value in vars(comic).values())
    with pytest.raises(archive.ArchiveError, match="关闭"):
        comic.prepare_page(0)


def test_resize_preserves_aspect_and_does_not_apply_exif_rotation():
    exif = Image.Exif()
    exif[274] = 6
    with archive.open_archive(
        zip_bytes([("a.jpg", picture("JPEG", (3000, 1500), exif=exif))])
    ) as comic:
        page = comic.prepare_page(0)
        assert (page.width, page.height) == (3000, 1500)
        assert (page.render_width, page.render_height) == (2400, 1200)
        with Image.open(io.BytesIO(page.jpeg_bytes)) as image:
            assert image.getexif().get(274) is None


@pytest.mark.parametrize(
    "name",
    [
        "../x.png",
        "a/../../x.png",
        "/x.png",
        "C:/x.png",
        "C:x.png",
        "\\\\server\\x.png",
        "a\\..\\x.png",
        "./x.png",
        "a//x.png",
        "dir/x\n.png",
        "dir/\x00x.png",
    ],
)
def test_rejects_unsafe_paths_even_for_non_image_entries(name):
    if "\x00" in name:
        raw = zip_bytes([("dir/xx.png", picture())])
        raw = raw.replace(b"dir/xx.png", b"dir/\x00x.png")
    else:
        raw = zip_bytes([(name, picture())])
    with pytest.raises(archive.ArchiveError):
        archive.open_archive(raw)
    with pytest.raises(archive.ArchiveError):
        archive.open_archive(
            zip_bytes([("normal.png", picture()), ("../ignored.txt", b"ignored")])
        )


@pytest.mark.parametrize(
    "names", [("a/1.png", "b/1.png"), ("1.png", "1.PNG"), ("a/é.png", "b/e\u0301.png")]
)
def test_duplicate_basename_is_rejected(names):
    with pytest.raises(archive.ArchiveError, match="同名"):
        archive.open_archive(zip_bytes([(name, picture()) for name in names]))


def test_symbolic_link_and_encryption_rejected():
    info = zipfile.ZipInfo("link.png")
    info.create_system = 3
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(archive.ArchiveError, match="符号链接"):
        archive.open_archive(zip_bytes([(info, b"elsewhere")]))
    raw = bytearray(zip_bytes([("a.png", picture())]))
    central = raw.index(b"PK\x01\x02")
    flags = struct.unpack_from("<H", raw, central + 8)[0]
    struct.pack_into("<H", raw, central + 8, flags | 1)
    with pytest.raises(archive.ArchiveError, match="加密"):
        archive.open_archive(bytes(raw))


def test_directory_count_checked_before_zipfile_allocation(monkeypatch):
    raw = bytearray(zip_bytes([("a.png", picture())]))
    eocd = raw.rindex(b"PK\x05\x06")
    struct.pack_into("<HH", raw, eocd + 8, 1001, 1001)
    monkeypatch.setattr(
        archive.zipfile,
        "ZipFile",
        lambda *a, **k: pytest.fail("must reject before allocation"),
    )
    with pytest.raises(archive.ArchiveError, match="1000"):
        archive.open_archive(bytes(raw))


def test_forged_directory_count_and_entry_limit(monkeypatch):
    raw = bytearray(zip_bytes([("a.png", picture()), ("b.png", picture())]))
    end = raw.rindex(b"PK\x05\x06")
    struct.pack_into("<HH", raw, end + 8, 1, 1)
    with pytest.raises(archive.ArchiveError, match="数量"):
        archive.open_archive(bytes(raw))
    monkeypatch.setattr(archive, "MAX_ENTRIES", 1)
    with pytest.raises(archive.ArchiveError):
        archive.open_archive(zip_bytes([("a.png", picture()), ("ignored.txt", b"x")]))


def test_limits_are_enforced_without_huge_allocations(monkeypatch):
    assert (
        archive.MAX_ARCHIVE_BYTES,
        archive.MAX_IMAGE_BYTES,
        archive.MAX_TOTAL_BYTES,
    ) == (40 * 1024 * 1024, 20 * 1024 * 1024, 200 * 1024 * 1024)
    assert (archive.MAX_PAGES, archive.MAX_ENTRIES, archive.MAX_PIXELS) == (
        100,
        1000,
        40_000_000,
    )
    raw = zip_bytes([("a.png", picture()), ("b.png", picture())])
    with monkeypatch.context() as m:
        m.setattr(archive, "MAX_ARCHIVE_BYTES", len(raw) - 1)
        with pytest.raises(archive.ArchiveError):
            archive.open_archive(raw)
    with monkeypatch.context() as m:
        m.setattr(archive, "MAX_IMAGE_BYTES", 2)
        with pytest.raises(archive.ArchiveError):
            archive.open_archive(raw)
    with monkeypatch.context() as m:
        m.setattr(archive, "MAX_TOTAL_BYTES", len(picture()))
        with pytest.raises(archive.ArchiveError):
            archive.open_archive(raw)
    with monkeypatch.context() as m:
        m.setattr(archive, "MAX_PAGES", 1)
        with pytest.raises(archive.ArchiveError):
            archive.open_archive(raw)
    with monkeypatch.context() as m:
        m.setattr(archive, "MAX_PIXELS", 31)
        with archive.open_archive(raw) as comic:
            with pytest.raises(archive.ArchiveError, match="像素"):
                comic.prepare_page(0)


@pytest.mark.parametrize("raw", [b"not zip", b"", b"PK\x03\x04broken"])
def test_non_zip_and_broken_archives_rejected(raw):
    with pytest.raises(archive.ArchiveError):
        archive.open_archive(raw)


def test_real_decode_rejects_bad_and_disguised_image():
    for body in (b"not an image", picture("GIF"), picture()[:20]):
        with archive.open_archive(zip_bytes([("fake.png", body)])) as comic:
            with pytest.raises(archive.ArchiveError):
                comic.prepare_page(0)


def test_actual_crc_is_checked():
    raw = bytearray(zip_bytes([("a.png", picture())]))
    central = raw.index(b"PK\x01\x02")
    struct.pack_into("<I", raw, central + 16, 0)
    with archive.open_archive(bytes(raw)) as comic:
        with pytest.raises(archive.ArchiveError):
            comic.prepare_page(0)


class RawFilename(zipfile.ZipInfo):
    def __init__(self, raw):
        super().__init__(raw.decode("cp437"))
        self.raw_name = raw

    def _encodeFilenameFlags(self):
        return self.raw_name, self.flag_bits & ~0x800


def test_chinese_legacy_name_strict_recovery_and_latin_preservation():
    with archive.open_archive(
        zip_bytes([(RawFilename("漫画/第1页.png".encode("gb18030")), picture())])
    ) as comic:
        assert comic.pages[0].logical_path == "漫画/第1页.png"
        assert comic.pages[0].basename == "第1页.png"
    with archive.open_archive(
        zip_bytes([(RawFilename("éá.png".encode("cp437")), picture())])
    ) as comic:
        assert comic.pages[0].basename == "éá.png"


def test_empty_archive_or_no_supported_images_rejected():
    with pytest.raises(archive.ArchiveError):
        archive.open_archive(zip_bytes([]))
    with pytest.raises(archive.ArchiveError):
        archive.open_archive(zip_bytes([("x.gif", picture("GIF"))]))


@pytest.mark.parametrize("compression", [zipfile.ZIP_BZIP2, zipfile.ZIP_LZMA])
def test_dictionary_unbounded_compression_is_rejected(compression):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=compression) as output:
        output.writestr("a.png", picture())
    with pytest.raises(archive.ArchiveError, match="压缩方式"):
        archive.open_archive(buffer.getvalue())


def test_transparent_image_flattens_onto_white_and_animated_image_rejected():
    buffer = io.BytesIO()
    with Image.new("RGBA", (8, 4), (255, 0, 0, 0)) as image:
        image.save(buffer, format="PNG")
        with archive.open_archive(zip_bytes([("a.png", buffer.getvalue())])) as comic:
            with Image.open(io.BytesIO(comic.prepare_page(0).jpeg_bytes)) as prepared:
                assert prepared.getpixel((0, 0)) == (255, 255, 255)
    first = Image.new("RGB", (4, 4), "red")
    second = Image.new("RGB", (4, 4), "blue")
    buffer = io.BytesIO()
    try:
        first.save(
            buffer, format="PNG", save_all=True, append_images=[second], duration=100
        )
    finally:
        first.close()
        second.close()
    with archive.open_archive(zip_bytes([("a.png", buffer.getvalue())])) as comic:
        with pytest.raises(archive.ArchiveError, match="多帧"):
            comic.prepare_page(0)


@pytest.fixture
def seven_zip():
    value = os.getenv("QQCOMIC_TEST_7Z") or shutil.which("7zz") or shutil.which("7z")
    if not value:
        pytest.skip("Set QQCOMIC_TEST_7Z to test the current official native decoder")
    return value


def listing_record(**changes):
    result = {
        "Path": "page.png",
        "Size": str(len(picture())),
        "Folder": "-",
        "Encrypted": "-",
        "Attributes": "A",
    }
    result.update(changes)
    return result


def listing_bytes(*records, header=None):
    header = header or {"Path": "archive.bin", "Type": "7z", "Physical Size": "6"}

    def encode(record):
        return "\n".join(f"{key} = {value}" for key, value in record.items())

    return (
        "7-Zip test\n--\n"
        + encode(header)
        + "\n\n----------\n"
        + "\n\n".join(encode(r) for r in records)
        + "\n"
    ).encode()


@pytest.mark.parametrize(
    "changes",
    [
        {"Path": "../page.png"},
        {"Path": "C:/page.png"},
        {"Path": "page.png:stream"},
        {"Path": "page\u2028.png"},
        {"Path": "page\x00.png"},
        {"Encrypted": "+"},
        {"Encrypted": ""},
        {"Symbolic Link": "other.png"},
        {"Hard Link": "other.png"},
        {"Copy Link": "other.png"},
        {"Alternate Stream": "+"},
        {"Split After": "+"},
        {"Split Before": "+"},
        {"Attributes": "A lrwxrwxrwx"},
        {"Attributes": "A brw-r--r--"},
        {"Size": "-1"},
        {"Size": "９"},
        {"Folder": "?"},
        {"Folder": "+", "Size": "1"},
    ],
)
def test_native_member_metadata_rejects_unsafe_or_ambiguous_entries(changes):
    with pytest.raises(archive.ArchiveError):
        archive._entry_metadata(listing_record(**changes))


@pytest.mark.parametrize(
    "body",
    [
        b"Path = page.png\nPath = duplicate.png\nSize = 1",
        b"Path = page.png\n\x00Size = 1",
        b"Path = page.png\n{\nSize = 1",
        b"Size = 1\nPath = page.png",
        b"Path = page.png\xc2\x85Size = 1",
    ],
)
def test_native_listing_rejects_duplicate_fields_and_record_injection(body):
    data = (
        listing_bytes(listing_record()).split(b"----------\n", 1)[0]
        + b"----------\n"
        + body
    )
    with pytest.raises(archive.ArchiveError):
        records = archive._listing(data, "7z", 6)
        for record in records:
            archive._entry_metadata(record)


@pytest.mark.parametrize(
    "changes",
    [
        {"Type": "Rar"},
        {"Physical Size": "7"},
        {"Volumes": "2"},
        {"Volume Index": "0"},
        {"Multivolume": "+"},
        {"Encrypted": "+"},
        {"Characteristics": "Volume Solid"},
        {"Offset": "1"},
        {"Tail Size": "2"},
    ],
)
def test_native_listing_rejects_wrong_format_multivolume_or_encryption(changes):
    header = {"Path": "archive.bin", "Type": "7z", "Physical Size": "6", **changes}
    with pytest.raises(archive.ArchiveError):
        archive._listing(listing_bytes(listing_record(), header=header), "7z", 6)


def test_native_listing_does_not_treat_multiline_comment_as_members():
    data = b"--\nPath = archive.bin\nType = 7z\nPhysical Size = 6\nComment = \n{\n----------\nPath = malicious.png\n}\n----------\n"
    with pytest.raises(archive.ArchiveError):
        archive._listing(data, "7z", 6)


def test_native_directory_preflight_keeps_existing_limits_and_cleans_temp(monkeypatch):
    calls = []
    monkeypatch.setattr(archive, "_decoder_path", lambda configured: "trusted-decoder")

    def run(command, cwd, output_limit):
        calls.append((command, cwd))
        return listing_bytes(
            listing_record(Path="a/page.png"), listing_record(Path="b/PAGE.png")
        )

    monkeypatch.setattr(archive, "_run_limited", run)
    with pytest.raises(archive.ArchiveError, match="同名"):
        archive.open_archive(b"7z\xbc\xaf\x27\x1c")
    assert not Path(calls[0][1]).exists()
    for name, value in [
        ("MAX_PAGES", 1),
        ("MAX_TOTAL_BYTES", 1),
        ("MAX_IMAGE_BYTES", 1),
        ("MAX_ENTRIES", 1),
    ]:
        with monkeypatch.context() as limited:
            limited.setattr(archive, name, value)
            with pytest.raises(archive.ArchiveError):
                archive.open_archive(b"7z\xbc\xaf\x27\x1c")
    assert all(not Path(cwd).exists() for _, cwd in calls)


def test_configured_decoder_path_never_falls_back_if_invalid(monkeypatch):
    monkeypatch.setattr(
        archive.shutil,
        "which",
        lambda name: pytest.fail("explicit path must not fall back"),
    )
    with pytest.raises(archive.ArchiveError, match="路径"):
        archive._decoder_path("relative/7z")
    # Existing ZIP support stays independent of native tools.
    with archive.open_archive(
        zip_bytes([("a.png", picture())]), "relative/invalid"
    ) as comic:
        assert comic.prepare_page(0).width == 8


def test_limited_process_does_not_inherit_secrets_and_uses_bounded_output(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("PRIVATE_TEST_TOKEN", "never-inherit")
    command = [
        getattr(sys, "_base_executable", sys.executable),
        "-I",
        "-S",
        "-c",
        "import os; print('PRIVATE_TEST_TOKEN' in os.environ)",
    ]
    assert archive._run_limited(command, str(tmp_path), 32).strip() == b"False"
    command[-1] = (
        "import sys; sys.stdout.buffer.write(b'x'*1000000); sys.stdout.flush()"
    )
    with pytest.raises(archive.ArchiveError, match="输出"):
        archive._run_limited(command, str(tmp_path), 64)


def test_limited_process_terminates_on_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "DECODER_TIMEOUT_SECONDS", 0.2)
    started = time.monotonic()
    with pytest.raises(archive.ArchiveError, match="120 秒"):
        archive._run_limited(
            [
                getattr(sys, "_base_executable", sys.executable),
                "-I",
                "-S",
                "-c",
                "import time; time.sleep(30)",
            ],
            str(tmp_path),
            64,
        )
    assert time.monotonic() - started < 5


def test_limited_process_enforces_memory_before_decoder_exec(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "DECODER_MEMORY_BYTES", 96 * 1024 * 1024)
    command = [
        getattr(sys, "_base_executable", sys.executable),
        "-I",
        "-S",
        "-c",
        "data=bytearray(128*1024*1024); print('must not succeed')",
    ]
    with pytest.raises(archive.ArchiveError, match="内存"):
        archive._run_limited(command, str(tmp_path), 64)


def test_posix_helper_applies_limits_before_exec_and_preserves_stricter_host_caps():
    calls = []

    class Stopped(BaseException):
        pass

    def leave(code):
        raise Stopped

    def execute(path, argv):
        calls.append(("exec", path, argv))
        raise Stopped

    fake_os = SimpleNamespace(name="posix", execv=execute, _exit=leave)
    fake_resource = SimpleNamespace(
        RLIMIT_AS=1,
        RLIMIT_CPU=2,
        RLIMIT_NOFILE=3,
        RLIMIT_CORE=4,
        RLIM_INFINITY=-1,
        getrlimit=lambda key: (0, {1: 256, 2: 30, 3: 32}[key]),
        setrlimit=lambda key, limits: calls.append((key, limits)),
    )
    fake_sys = SimpleNamespace(argv=["helper", "512", "120", "/trusted/7zz", "l"])
    real_import = builtins.__import__

    def imports(name, *args, **kwargs):
        return {"os": fake_os, "sys": fake_sys, "resource": fake_resource}.get(
            name
        ) or real_import(name, *args, **kwargs)

    namespace = {"__builtins__": {**vars(builtins), "__import__": imports}}
    with pytest.raises(Stopped):
        exec(archive._LIMITED_WORKER, namespace)
    assert calls == [
        (1, (256, 256)),
        (2, (30, 30)),
        (3, (32, 32)),
        (4, (0, 0)),
        ("exec", "/trusted/7zz", ["/trusted/7zz", "l"]),
    ]


@pytest.mark.parametrize("version", [4, 5])
def test_real_solid_rar_images_and_temp_cleanup(seven_zip, version):
    with archive.open_archive(
        base64.b64decode(SYNTHETIC_RAR_SOLID[version]), seven_zip
    ) as comic:
        temp = Path(comic._temporary.name)
        assert [p.basename for p in comic.pages] == ["page2.png", "page10.png"]
        assert [comic.prepare_page(i).width for i in range(2)] == [8, 8]
        assert [p.name for p in temp.iterdir()] == ["archive.bin"]
    assert not temp.exists()


def test_real_solid_7z_with_option_like_member_names(seven_zip, tmp_path):
    names = ["page2.png", "page10.png", "@page.png", "-page.png", "literal.png"]
    for name in names:
        (tmp_path / name).write_bytes(picture())
    target = tmp_path / "synthetic.7z"
    archive._run_limited(
        [seven_zip, "a", "-t7z", "-ms=on", "-mmt=1", "--", str(target), *names],
        str(tmp_path),
        16384,
    )
    # The host filesystem may prohibit '*'; 7z can still store that literal name.
    archive._run_limited(
        [seven_zip, "rn", "-spd", "--", str(target), "literal.png", "a[1]*.png"],
        str(tmp_path),
        16384,
    )
    with archive.open_archive(target.read_bytes(), seven_zip) as comic:
        assert [page.basename for page in comic.pages] == [
            "-page.png",
            "@page.png",
            "a[1]*.png",
            "page2.png",
            "page10.png",
        ]
        assert all(comic.prepare_page(i).width == 8 for i in range(5))


# Synthetic 8x4 white PNGs, encoded offline as solid RAR4/RAR5.
SYNTHETIC_RAR_SOLID = {
    4: "UmFyIRoHADvQcwgADQAAAAAAAABRe3SAkC8AaQAAAE0AAAACzMj3DgRRVF0dNQoAIAAAAHBhZ2UxMC5wbmcA8B6tjA2ZmQfkz6OJOdJ1apgCpcqSITkOp6IhekYSoVIpXrFCtWYKxB2AMHScrWf4Qv++9o9k9yTzAPMVyoTMYrpRfBkTnpAkDMkYD4eO9gDiDx1XaW+LXEODnGvN9sTyhtKv61B4kSpPvJZh6HqGdJCQLgADAAAATQAAAALMyPcOBFFUXR01CQAgAAAAcGFnZTIucG5nAPAerYz+p9TEPXsAQAcA",
    5: "UmFyIRoHAQAgtvoRCgEFBgQFAQGAgACQ2KaqJgIDC+oABM0AIMzI9w6AHQAKcGFnZTEwLnBuZwoDAp73rNo3YN0Bx/pnJVVTL5M/jFCMCVtTgDU3NSCKRFlMERb0HCWhqRpb2xoa2tcFcQZgDg6Es/g7h/vdo6Hco5gHMk0qVQcz5gADppRWJKObK0Px9e8CHkolsw2v+YyQkXedusLoQLI4vHzcXgmdSOZrslDZkUQlAgMLhQAEzQAgzMj3DsAdAAlwYWdlMi5wbmcKAwKe96zaN2DdAUUdAv7kHXdWUQMFBAA=",
}
