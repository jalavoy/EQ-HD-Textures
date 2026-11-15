import struct

import pytest

from eq_hd import pfs


def test_name_crc_matches_shipped_archives():
    # values read from the directory of a real nexus.s3d
    assert pfs.name_crc("nexus.wld") == 0xAA092607
    assert pfs.name_crc("palette.bmp") == 0xF2D00EAB
    assert pfs.name_crc("PALETTE.BMP") == pfs.name_crc("palette.bmp")


@pytest.mark.parametrize("marker", pfs.NAMES_CRCS)
def test_roundtrip_keeps_order_marker_and_contents(marker):
    files = {"b.wld": b"x" * 20000, "a.dds": bytes(range(256)) * 100, "empty.txt": b""}
    archive = pfs.read(pfs.write(pfs.Archive(files, marker)))
    assert list(archive.files) == list(files)
    assert archive.files == files
    assert archive.names_crc == marker


def test_mismatched_crc_and_name_case_survive_a_repack():
    raw = pfs.write(pfs.Archive({"zom (New).bmp": b"abc", "ok.bmp": b"def"},
                                crc_overrides={"zom (New).bmp": 0x12345678}))
    archive = pfs.read(raw)
    assert "zom (New).bmp" in archive.files
    assert archive.crc_overrides == {"zom (New).bmp": 0x12345678}
    assert pfs.write(archive) == raw


def test_directory_is_sorted_by_crc():
    raw = pfs.write(pfs.Archive({f"f{i}.bin": bytes([i]) for i in range(20)}))
    dir_off = struct.unpack_from("<I", raw, 0)[0]
    n = struct.unpack_from("<I", raw, dir_off)[0]
    crcs = [struct.unpack_from("<I", raw, dir_off + 4 + i * 12)[0] for i in range(n)]
    assert crcs == sorted(crcs)


@pytest.mark.parametrize("raw", [b"", b"not an archive at all", b"\x00" * 64])
def test_rejects_garbage(raw):
    with pytest.raises(pfs.PfsError):
        pfs.read(raw)


def test_marker_only_when_asked():
    a = pfs.Archive({"x.bmp": b"1"})
    assert pfs.has_marker(pfs.write(a, marker=True))
    assert not pfs.has_marker(pfs.write(a))
    assert pfs.read(pfs.write(a, marker=True)).files == a.files
