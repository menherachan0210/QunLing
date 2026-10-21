"""Passwords are ephemeral inputs; encrypted archives retain all existing bounds."""

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from qunling.files import FileError, FileLimits, inspect_file, read_archive_member, read_file, search_file
from qunling.files.archives import archive_password, validate_native_listing
from qunling.files.decoder import _run


# Synthetic ZipCrypto archive, note.txt="attachment password test". No native
# program is needed to exercise the common encrypted ZIP path in regular CI.
ZIP_CRYPTO = (
    "UEsDBBQAAQAAAENPVV36aGugJAAAABgAAAAIAAAAbm90ZS50eHRECedNTDXf0ZwLtKbRan6o"
    "fm1MRkbSG38J8ym1IqJOo7tpxxBQSwECPwAUAAEAAABDT1Vd+mhroCQAAAAYAAAACAAk"
    "AAAAAAAAACAAAAAAAAAAbm90ZS50eHQKACAAAAAAAAEAGAD8zAOd/2DdAQAAAAAAAAAAAAAA"
    "AAAAAABQSwUGAAAAAAEAAQBaAAAASgAAAAAA"
)


@pytest.fixture
def encrypted_zip(tmp_path):
    path = tmp_path / "encrypted.zip"
    path.write_bytes(base64.b64decode(ZIP_CRYPTO))
    return path


def test_zip_password_retry_and_all_public_reading_operations(encrypted_zip):
    for password, code in [(None, "archive_password_required"),
                           ("wrong-private-input", "archive_password_invalid")]:
        with pytest.raises(FileError) as error:
            inspect_file(encrypted_zip, password=password)
        assert error.value.code == code
        assert password is None or password not in str(error.value)
    password = "fixture-pass"
    manifest = inspect_file(encrypted_zip, password=password)
    assert manifest["format"] == "zip" and manifest["structure"][0]["members"] == 1
    listing = read_file(encrypted_zip, password=password)
    assert listing["sections"][0]["entries"][0]["path"] == "note.txt"
    text = read_file(encrypted_zip, {"member": "note.txt"}, password=password)
    assert text["sections"][0]["text"] == "attachment password test"
    assert search_file(encrypted_zip, "note", password=password)["hits"]
    assert read_archive_member(encrypted_zip, "note.txt", password=password) == b"attachment password test"
    assert password not in json.dumps([manifest, listing, text])
    with pytest.raises(FileError) as error:
        read_file(encrypted_zip, {"password": password})
    assert error.value.code == "invalid_schema"


def test_encrypted_zip_limits_still_checked(encrypted_zip):
    with pytest.raises(FileError) as error:
        inspect_file(encrypted_zip, password="fixture-pass",
                     limits=replace(FileLimits(), max_member_bytes=5))
    assert error.value.code == "member_limit"


@pytest.mark.parametrize("password", ["", 4, True, "line\nbreak", "carriage\rreturn", "nul\0", "x" * 1025, "\ud800"])
def test_password_validation_has_no_value_in_errors(password):
    with pytest.raises(FileError) as error:
        archive_password(password)
    assert error.value.code == "invalid_archive_password"
    assert str(password) not in str(error.value) if password else True


def test_password_keeps_spaces_and_unicode():
    assert archive_password(" 空 格 ") == " 空 格 "


def test_encrypted_listing_retains_member_guards():
    prefix = "--\nPath = archive\nType = 7z\nPhysical Size = 100\n\n----------\n"
    member = "Path = ../escape\nSize = 3\nPacked Size = 3\nFolder = -\nEncrypted = +\n"
    with pytest.raises(FileError) as error:
        validate_native_listing((prefix + member).encode(), "7z", 100, FileLimits(), password_supplied=True)
    assert error.value.code == "unsafe_member"
    member = member.replace("../escape", "safe.txt")
    result = validate_native_listing((prefix + member).encode(), "7z", 100, FileLimits(), password_supplied=True)
    assert result[0]["_encrypted"]


@pytest.fixture
def native_limits():
    executable = os.environ.get("QUNLING_TEST_7ZIP") or shutil.which("7zz") or shutil.which("7z")
    if not executable:
        pytest.skip("Optional native 7-Zip executable is not installed")
    return replace(FileLimits(), seven_zip_path=str(Path(executable).resolve()))


@pytest.mark.parametrize("kind,switches,password", [
    ("zip", ["-mem=ZipCrypto"], "native pass"),
    ("zip", ["-mem=AES256"], "native pass"),
    ("7z", [], "native pass"),
    ("7z", ["-mhe=on"], " 中文 secret with spaces "),
])
def test_native_encryption_roundtrip_no_argv_credentials_or_temp_residue(
    tmp_path, native_limits, monkeypatch, kind, switches, password,
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "data.txt").write_bytes(b"synthetic archive content")
    archive = tmp_path / f"encrypted.{kind}"
    subprocess.run([native_limits.seven_zip_path, "a", "-t" + kind, "-p" + password,
                    *switches, str(archive), "data.txt"],
                   cwd=source, capture_output=True, check=True, timeout=20)
    from qunling.files import decoder
    launch = decoder.subprocess.Popen
    directories = []
    original_temp = tempfile.TemporaryDirectory

    def recording_launch(args, **kwargs):
        assert not any(password in str(arg) for arg in args)
        assert not any(password in str(value) for value in kwargs.get("env", {}).values())
        return launch(args, **kwargs)

    def record_temp(*args, **kwargs):
        directory = original_temp(*args, **kwargs)
        directories.append(directory.name)
        return directory

    monkeypatch.setattr(decoder.subprocess, "Popen", recording_launch)
    monkeypatch.setattr(decoder.tempfile, "TemporaryDirectory", record_temp)
    for supplied, code in [(None, "archive_password_required"), ("wrong", "archive_password_invalid")]:
        with pytest.raises(FileError) as error:
            inspect_file(archive, password=supplied, limits=native_limits)
        assert error.value.code == code
    result = read_archive_member(archive, "data.txt", password=password, limits=native_limits)
    assert result == b"synthetic archive content"
    assert all(not Path(path).exists() for path in directories)
    assert list(source.iterdir()) == [source / "data.txt"]


@pytest.mark.parametrize("filename", [
    "test_read_format_rar4_encrypted.rar",
    "test_read_format_rar4_encrypted_filenames.rar",
    "test_read_format_rar4_solid_encrypted.rar",
    "test_read_format_rar5_encrypted.rar",
    "test_read_format_rar5_encrypted_filenames.rar",
])
def test_optional_rar_data_and_header_passwords(native_limits, filename):
    directory = os.environ.get("QUNLING_TEST_ARCHIVES")
    if not directory or not (Path(directory) / filename).is_file():
        pytest.skip("Optional native archive fixtures not installed")
    archive = Path(directory) / filename
    for supplied, code in [(None, "archive_password_required"), ("wrong", "archive_password_invalid")]:
        with pytest.raises(FileError) as error:
            inspect_file(archive, password=supplied, limits=native_limits)
        assert error.value.code == code
    value = read_file(archive, password="password", limits=native_limits)
    assert value["format"] == "rar"
    member = next(item for item in value["sections"][0]["entries"] if item["path"] == "b.txt")
    assert len(read_archive_member(archive, member["path"], password="password", limits=native_limits)) == member["size"]


def test_native_password_error_is_redacted(tmp_path):
    command = [getattr(sys, "_base_executable", sys.executable), "-c",
               "import sys; sys.stderr.write('Wrong password? private-filename'); sys.exit(2)"]
    with pytest.raises(FileError) as error:
        _run(command, str(tmp_path), 100, FileLimits(), password="private-credential")
    assert error.value.code == "archive_password_invalid"
    assert "private" not in str(error.value)


def test_native_password_input_does_not_disable_output_or_timeout_bounds(tmp_path):
    with pytest.raises(FileError) as error:
        _run([getattr(sys, "_base_executable", sys.executable), "-c", "print('x'*10000)"], str(tmp_path), 10,
             FileLimits(), password="transient")
    assert error.value.code == "archive_limit"
    with pytest.raises(FileError) as error:
        _run([getattr(sys, "_base_executable", sys.executable), "-c", "import time; time.sleep(5)"], str(tmp_path), 10,
             replace(FileLimits(), decoder_seconds=1), password="transient")
    assert error.value.code == "decoder_timeout"
