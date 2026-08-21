from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
import os
from pathlib import Path
import stat

from browser_public_identity import mask_profile_id


_SENSITIVE_KEY_PARTS = (
    "cookie",
    "authorization",
    "token",
    "secret",
)
_SENSITIVE_KEYS = {
    "comment_text",
    "input_text",
    "cdp_url",
    "ws_url",
    "dom",
    "html",
    "accessibility_tree",
}
_OVERLAY_ATTRIBUTE = "data-selector-probe-redaction"
_INSTALL_OVERLAYS = f"""
regions => {{
    for (const region of regions) {{
        const node = document.createElement('div');
        node.setAttribute('{_OVERLAY_ATTRIBUTE}', '1');
        node.style.position = 'fixed';
        node.style.left = `${{region.x}}px`;
        node.style.top = `${{region.y}}px`;
        node.style.width = `${{region.width}}px`;
        node.style.height = `${{region.height}}px`;
        node.style.background = '#000';
        node.style.opacity = '1';
        node.style.zIndex = '2147483647';
        node.style.pointerEvents = 'none';
        document.documentElement.appendChild(node);
    }}
}}
""".strip()
_REMOVE_OVERLAYS = f"""
() => {{
    for (const node of document.querySelectorAll(
        '[{_OVERLAY_ATTRIBUTE}="1"]'
    )) {{
        node.remove();
    }}
}}
""".strip()
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE_ROOT = (
    PROJECT_ROOT / "data" / "selector-probe-evidence"
)


def _sensitive_key(value: object) -> bool:
    if not isinstance(value, str):
        return False
    normalized = value.strip().casefold().replace("-", "_")
    return (
        normalized in _SENSITIVE_KEYS
        or any(part in normalized for part in _SENSITIVE_KEY_PARTS)
        or any(
            normalized.endswith(f"_{suffix}")
            for suffix in ("dom", "html", "accessibility_tree")
        )
    )


def redact_evidence(
    value: object,
    profile_ids: Sequence[str] = (),
) -> object:
    profiles = sorted(
        (
            (item, mask_profile_id(item))
            for item in profile_ids
            if isinstance(item, str) and item
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )

    def mask_profiles(text: str) -> str:
        result = text
        for profile_id, masked in profiles:
            result = result.replace(profile_id, masked)
        return result

    def redact(current: object) -> object:
        if isinstance(current, str):
            return mask_profiles(current)[:500]
        if current is None or isinstance(current, (bool, int)):
            return current
        if isinstance(current, float):
            return current if math.isfinite(current) else None
        if isinstance(current, Mapping):
            result: dict[str, object] = {}
            for key, item in current.items():
                if len(result) >= 100:
                    break
                if not isinstance(key, str) or _sensitive_key(key):
                    continue
                result[mask_profiles(key)[:128]] = redact(item)
            return result
        if isinstance(current, Sequence) and not isinstance(
            current,
            (str, bytes, bytearray),
        ):
            return [redact(item) for item in current[:50]]
        return mask_profiles(str(current))[:500]

    return redact(value)


def _expanded_regions(regions: object) -> list[dict[str, float]]:
    if not isinstance(regions, Sequence) or isinstance(
        regions,
        (str, bytes, bytearray),
    ):
        raise ValueError("regions must be an array")
    result: list[dict[str, float]] = []
    for item in regions:
        if not isinstance(item, Mapping):
            raise ValueError("redaction region must be an object")
        values = [item.get(key) for key in ("x", "y", "width", "height")]
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            for value in values
        ):
            raise ValueError("redaction region must contain finite coordinates")
        x, y, width, height = (float(value) for value in values)
        if width <= 0 or height <= 0:
            raise ValueError("redaction region must have positive dimensions")
        left = max(0.0, x - 4.0)
        top = max(0.0, y - 4.0)
        result.append(
            {
                "x": left,
                "y": top,
                "width": width + (x - left) + 4.0,
                "height": height + (y - top) + 4.0,
            }
        )
    return result


def _is_link_or_junction(path: Path) -> bool:
    is_junction = getattr(path, "is_junction", None)
    return path.is_symlink() or (
        callable(is_junction) and is_junction()
    )


def resolve_evidence_path(
    evidence_root: str | Path,
    value: str | Path,
    *,
    must_exist: bool,
) -> Path:
    root_input = Path(evidence_root)
    if _is_link_or_junction(root_input):
        raise ValueError("evidence root cannot be a link")
    root_input.mkdir(parents=True, exist_ok=True)
    root = root_input.resolve(strict=True)
    if _is_link_or_junction(root):
        raise ValueError("evidence root cannot be a link")
    raw = Path(value)
    candidate = raw if raw.is_absolute() else root / raw
    lexical = Path(os.path.abspath(candidate))
    try:
        relative = lexical.relative_to(root)
    except ValueError as error:
        raise ValueError("evidence path escapes evidence root") from error
    if len(relative.parts) != 1 or relative.name in {"", ".", ".."}:
        raise ValueError("evidence files must be direct children of evidence root")
    resolved = lexical.resolve(strict=must_exist)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError("evidence path escapes evidence root") from error
    if must_exist and (
        not resolved.is_file() or _is_link_or_junction(lexical)
    ):
        raise ValueError("evidence path must be a regular file")
    return resolved


def _lexical_evidence_target(
    evidence_root: str | Path,
    value: str | Path,
) -> tuple[Path, Path]:
    root = Path(os.path.abspath(Path(evidence_root)))
    raw = Path(value)
    target = Path(os.path.abspath(raw if raw.is_absolute() else root / raw))
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise ValueError("evidence path escapes evidence root") from error
    if len(relative.parts) != 1 or relative.name in {"", ".", ".."}:
        raise ValueError("evidence files must be direct children of evidence root")
    return root, target


def _windows_final_path(handle: int) -> Path:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.GetFinalPathNameByHandleW
    function.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    function.restype = wintypes.DWORD
    size = function(handle, None, 0, 0)
    if not size:
        raise OSError(ctypes.get_last_error(), "cannot resolve evidence handle")
    buffer = ctypes.create_unicode_buffer(size + 1)
    written = function(handle, buffer, len(buffer), 0)
    if not written or written >= len(buffer):
        raise OSError(ctypes.get_last_error(), "cannot resolve evidence handle")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def _windows_dispose(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    class FileDispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOL)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.SetFileInformationByHandle
    function.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    function.restype = wintypes.BOOL
    value = FileDispositionInfo(True)
    if not function(handle, 4, ctypes.byref(value), ctypes.sizeof(value)):
        raise OSError(ctypes.get_last_error(), "cannot delete evidence file")


def _windows_close(handle: int) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.CloseHandle
    function.argtypes = [wintypes.HANDLE]
    function.restype = wintypes.BOOL
    if not function(handle):
        raise OSError(ctypes.get_last_error(), "cannot close evidence handle")


def _windows_attributes(handle: int) -> int:
    import ctypes
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    function = kernel32.GetFileInformationByHandle
    function.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    ]
    function.restype = wintypes.BOOL
    information = ByHandleFileInformation()
    if not function(handle, ctypes.byref(information)):
        raise OSError(ctypes.get_last_error(), "cannot inspect evidence handle")
    return int(information.file_attributes)


def _windows_create_file(
    target: Path,
    *,
    create: bool,
    directory: bool = False,
    share_delete: bool = True,
) -> int:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(target),
        (0x40000000 if create else 0)
        | (0 if directory else 0x00010000),
        0x00000001
        | 0x00000002
        | (0x00000004 if share_delete else 0),
        None,
        1 if create else 3,
        0x00200000
        | 0x00000080
        | (0x02000000 if directory else 0),
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        raise OSError(ctypes.get_last_error(), "cannot open evidence file")
    return int(handle)


def _windows_open_root(root: Path) -> tuple[int, Path]:
    handle = _windows_create_file(
        root,
        create=False,
        directory=True,
        share_delete=False,
    )
    try:
        attributes = _windows_attributes(handle)
        if (
            not attributes & 0x00000010
            or attributes & 0x00000400
        ):
            raise ValueError("evidence root cannot be a link")
        final = _windows_final_path(handle)
        if os.path.normcase(str(final)) != os.path.normcase(str(root)):
            raise ValueError("evidence root changed during access")
        return handle, final
    except BaseException:
        _windows_close(handle)
        raise


def _windows_open_target(
    root_handle: int,
    root: Path,
    target: Path,
    *,
    create: bool,
) -> int:
    handle = _windows_create_file(
        target,
        create=create,
        share_delete=False,
    )
    try:
        attributes = _windows_attributes(handle)
        if attributes & (0x00000010 | 0x00000400):
            if create:
                _windows_dispose(handle)
            raise ValueError("evidence path must be a regular file")
        final = _windows_final_path(handle)
        current_root = _windows_final_path(root_handle)
        if (
            os.path.normcase(str(current_root))
            != os.path.normcase(str(root))
            or os.path.normcase(str(final.parent))
            != os.path.normcase(str(current_root))
            or final.name != target.name
        ):
            if create:
                _windows_dispose(handle)
            raise ValueError("evidence target changed during access")
        return handle
    except BaseException:
        _windows_close(handle)
        raise


def _posix_open_root(root: Path) -> tuple[int, tuple[int, int]]:
    descriptor = os.open(
        root,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISDIR(opened.st_mode):
            raise ValueError("evidence root must be a directory")
        current = os.stat(root, follow_symlinks=False)
        identity = (opened.st_dev, opened.st_ino)
        if (
            stat.S_ISLNK(current.st_mode)
            or (current.st_dev, current.st_ino) != identity
        ):
            raise ValueError("evidence root changed during access")
        return descriptor, identity
    except BaseException:
        os.close(descriptor)
        raise


def _posix_root_matches(
    root: Path,
    identity: tuple[int, int],
) -> bool:
    try:
        current = os.stat(root, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISDIR(current.st_mode)
        and not stat.S_ISLNK(current.st_mode)
        and (current.st_dev, current.st_ino) == identity
    )


def _posix_open_target(root_descriptor: int, name: str) -> int:
    return os.open(
        name,
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0),
        0o600,
        dir_fd=root_descriptor,
    )


def _posix_delete_target(
    root_descriptor: int,
    name: str,
    root: Path,
    identity: tuple[int, int],
) -> None:
    if not _posix_root_matches(root, identity):
        raise ValueError("evidence root changed during access")
    os.unlink(name, dir_fd=root_descriptor)


def _write_new_evidence(
    root: Path,
    target: Path,
    value: bytes,
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    root, checked = _lexical_evidence_target(root, target)
    if os.name == "nt":
        import msvcrt

        root_handle, opened_root = _windows_open_root(root)
        try:
            handle = _windows_open_target(
                root_handle,
                opened_root,
                checked,
                create=True,
            )
            descriptor = msvcrt.open_osfhandle(handle, os.O_WRONLY)
            try:
                with os.fdopen(descriptor, "wb", closefd=False) as stream:
                    stream.write(value)
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                os.close(descriptor)
        finally:
            _windows_close(root_handle)
        return
    root_descriptor, identity = _posix_open_root(root)
    descriptor = -1
    try:
        descriptor = _posix_open_target(root_descriptor, checked.name)
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("evidence target must be a regular file")
        if not _posix_root_matches(root, identity):
            os.close(descriptor)
            descriptor = -1
            os.unlink(checked.name, dir_fd=root_descriptor)
            raise ValueError("evidence root changed during access")
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        if not _posix_root_matches(root, identity):
            os.close(descriptor)
            descriptor = -1
            os.unlink(checked.name, dir_fd=root_descriptor)
            raise ValueError("evidence root changed during access")
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(root_descriptor)


def delete_evidence_file(
    evidence_root: str | Path,
    value: str | Path,
) -> bool:
    root, target = _lexical_evidence_target(evidence_root, value)
    if os.name == "nt":
        root_handle, opened_root = _windows_open_root(root)
        try:
            handle = _windows_open_target(
                root_handle,
                opened_root,
                target,
                create=False,
            )
            try:
                _windows_dispose(handle)
            finally:
                _windows_close(handle)
        finally:
            _windows_close(root_handle)
        return True
    root_descriptor, identity = _posix_open_root(root)
    try:
        opened = os.stat(
            target.name,
            dir_fd=root_descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("evidence path must be a regular file")
        _posix_delete_target(
            root_descriptor,
            target.name,
            root,
            identity,
        )
        return True
    finally:
        os.close(root_descriptor)


def _has_forbidden_jpeg_metadata(value: bytes) -> bool:
    if len(value) < 4 or value[:2] != b"\xff\xd8":
        return True
    cursor = 2
    while cursor + 1 < len(value):
        if value[cursor] != 0xFF:
            cursor += 1
            continue
        marker = value[cursor + 1]
        cursor += 2
        if marker in {0xD9, 0xDA}:
            break
        if marker in {0x00, 0x01} or 0xD0 <= marker <= 0xD8:
            continue
        if cursor + 2 > len(value):
            return True
        length = int.from_bytes(value[cursor:cursor + 2], "big")
        if length < 2 or cursor + length > len(value):
            return True
        segment = value[cursor + 2:cursor + length]
        if marker == 0xFE:
            return True
        if marker in {0xE1, 0xED} and (
            b"exif" in segment.lower()
            or b"xmp" in segment.lower()
            or b"http://ns.adobe.com/" in segment.lower()
        ):
            return True
        cursor += length
    return False


async def capture_redacted_screenshot(
    page: object,
    regions: object,
    target_path: str | Path,
    *,
    evidence_root: str | Path = DEFAULT_EVIDENCE_ROOT,
) -> Path:
    evaluate = getattr(page, "evaluate", None)
    screenshot = getattr(page, "screenshot", None)
    if not callable(evaluate) or not callable(screenshot):
        raise TypeError("page must support evaluate and screenshot")
    expanded = _expanded_regions(regions)
    _root, target = _lexical_evidence_target(
        evidence_root,
        target_path,
    )
    if target.suffix.casefold() not in {".jpg", ".jpeg"}:
        raise ValueError("redacted screenshot target must be JPEG")
    try:
        await evaluate(_INSTALL_OVERLAYS, expanded)
        encoded = await screenshot(type="jpeg", quality=70)
        if not isinstance(encoded, bytes):
            raise RuntimeError("screenshot did not return JPEG bytes")
        if _has_forbidden_jpeg_metadata(encoded):
            raise RuntimeError("redacted screenshot contains metadata")
        _write_new_evidence(Path(evidence_root), target, encoded)
        return target
    finally:
        await evaluate(_REMOVE_OVERLAYS)


__all__ = [
    "DEFAULT_EVIDENCE_ROOT",
    "capture_redacted_screenshot",
    "delete_evidence_file",
    "redact_evidence",
    "resolve_evidence_path",
]
