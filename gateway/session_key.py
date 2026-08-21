"""Private, create-once Flask session signing key."""

from __future__ import annotations

import errno
import os
from pathlib import Path
import secrets
import stat
import time


_KEY_BYTES = 64
_MIN_KEY_LENGTH = 80
_READ_RETRIES = 100
_READ_RETRY_SECONDS = 0.01
_TRANSIENT_CONTENT_ERRORS = {
    "session_key_empty",
    "session_key_too_short",
}


def load_or_create_session_key(path: Path) -> str:
    key_path = _validated_key_path(path)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        return _load_or_create_windows(key_path)
    return _load_or_create_posix(key_path)


def _validated_key_path(path: Path) -> Path:
    key_path = Path(path).absolute()
    if (
        key_path.name in {"", ".", ".."}
        or key_path.parent == key_path
        or (os.name == "nt" and ":" in key_path.name)
    ):
        raise RuntimeError("session_key_unsafe_path")
    return key_path


def _load_or_create_posix(path: Path) -> str:
    ancestor_chain = _open_posix_ancestor_chain(path.parent)
    try:
        try:
            os.fchmod(
                ancestor_chain[-1][1],
                stat.S_IRWXU,
            )
        except OSError as error:
            raise RuntimeError(
                "session_key_parent_permissions"
            ) from error
        return _posix_retry_loop(
            path,
            ancestor_chain[-1][1],
            ancestor_chain,
        )
    finally:
        _close_posix_ancestor_chain(ancestor_chain)


def _open_posix_ancestor_chain(path: Path):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    chain = []
    current_path = Path(path.anchor)
    try:
        descriptor = os.open(current_path, flags)
        status = os.fstat(descriptor)
        _validate_parent_status(status)
        chain.append((current_path, descriptor, _identity(status)))
        for component in path.parts[1:]:
            current_path = current_path / component
            try:
                descriptor = os.open(
                    component,
                    flags,
                    dir_fd=chain[-1][1],
                )
            except OSError as error:
                if error.errno == errno.ELOOP:
                    raise RuntimeError(
                        "session_key_unsafe_path"
                    ) from error
                raise
            status = os.fstat(descriptor)
            _validate_parent_status(status)
            chain.append((current_path, descriptor, _identity(status)))
        return chain
    except BaseException:
        _close_posix_ancestor_chain(chain)
        raise


def _close_posix_ancestor_chain(chain) -> None:
    for _path, descriptor, _identity_value in reversed(chain):
        os.close(descriptor)


def _posix_retry_loop(
    path: Path,
    parent_descriptor: int,
    ancestor_chain,
) -> str:
    last_transient_error = None
    for _attempt in range(_READ_RETRIES):
        descriptor = -1
        created = False
        try:
            no_follow = getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(
                    path.name,
                    os.O_RDONLY | no_follow,
                    dir_fd=parent_descriptor,
                )
            except FileNotFoundError:
                try:
                    descriptor = os.open(
                        path.name,
                        os.O_RDWR | no_follow | os.O_CREAT | os.O_EXCL,
                        stat.S_IRUSR | stat.S_IWUSR,
                        dir_fd=parent_descriptor,
                    )
                    created = True
                except FileExistsError:
                    time.sleep(_READ_RETRY_SECONDS)
                    continue
            opened_status = os.fstat(descriptor)
            _validate_target_status(opened_status)
            opened_identity = _identity(opened_status)
            if created:
                _write_new_key_to_descriptor(descriptor)
            _tighten_descriptor_permissions(descriptor)
            value = _read_key_from_descriptor(descriptor)
            _verify_posix_target(
                parent_descriptor,
                path.name,
                opened_identity,
            )
            _verify_posix_ancestor_chain(ancestor_chain)
            return value
        except RuntimeError as error:
            if created:
                _cleanup_posix_created(
                    parent_descriptor,
                    path.name,
                    descriptor,
                )
            if str(error) not in _TRANSIENT_CONTENT_ERRORS:
                raise
            last_transient_error = error
            time.sleep(_READ_RETRY_SECONDS)
        except BaseException:
            if created:
                _cleanup_posix_created(
                    parent_descriptor,
                    path.name,
                    descriptor,
                )
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    if last_transient_error is not None:
        raise last_transient_error
    raise RuntimeError("session_key_creation_race")


def _verify_posix_target(
    parent_descriptor: int,
    name: str,
    expected_identity,
) -> None:
    try:
        status = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except FileNotFoundError as error:
        raise RuntimeError("session_key_unsafe_path") from error
    _validate_target_status(status)
    if _identity(status) != expected_identity:
        raise RuntimeError("session_key_unsafe_path")


def _verify_posix_ancestor_chain(chain) -> None:
    current_chain = _open_posix_ancestor_chain(chain[-1][0])
    try:
        current_identities = [
            identity_value
            for _path, _descriptor, identity_value in current_chain
        ]
        expected_identities = [
            identity_value
            for _path, _descriptor, identity_value in chain
        ]
        if current_identities != expected_identities:
            raise RuntimeError("session_key_unsafe_path")
    finally:
        _close_posix_ancestor_chain(current_chain)


def _cleanup_posix_created(
    parent_descriptor: int,
    name: str,
    descriptor: int,
) -> None:
    if descriptor < 0:
        return
    try:
        os.ftruncate(descriptor, 0)
        os.fsync(descriptor)
    except OSError as error:
        raise RuntimeError("session_key_cleanup_failed") from error
    expected_identity = _identity(os.fstat(descriptor))
    try:
        status = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if _identity(status) == expected_identity:
            os.unlink(name, dir_fd=parent_descriptor)
    except FileNotFoundError:
        pass


def _write_new_key_to_descriptor(descriptor: int) -> str:
    value = secrets.token_urlsafe(_KEY_BYTES)
    payload = (value + "\n").encode("utf-8")
    os.lseek(descriptor, 0, os.SEEK_SET)
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError("session_key_write_failed")
        offset += written
    os.ftruncate(descriptor, len(payload))
    os.fsync(descriptor)
    return value


def _read_key_from_descriptor(descriptor: int) -> str:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = []
    while True:
        chunk = os.read(descriptor, 4096)
        if not chunk:
            break
        chunks.append(chunk)
    try:
        raw = b"".join(chunks).decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError("session_key_invalid_encoding") from error
    value = raw.strip()
    if not value:
        raise RuntimeError("session_key_empty")
    if len(value) < _MIN_KEY_LENGTH:
        raise RuntimeError("session_key_too_short")
    if "\n" in value or "\r" in value:
        raise RuntimeError("session_key_invalid_newline")
    return value


def _tighten_descriptor_permissions(descriptor: int) -> None:
    try:
        if os.name == "nt":
            _set_windows_descriptor_permissions(descriptor)
            os.chmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
        else:
            os.fchmod(descriptor, stat.S_IRUSR | stat.S_IWUSR)
    except OSError as error:
        raise RuntimeError("session_key_permissions") from error


def _validate_parent_status(status: os.stat_result) -> None:
    if _is_reparse_or_symlink(status) or not stat.S_ISDIR(status.st_mode):
        raise RuntimeError("session_key_unsafe_path")


def _validate_target_status(status: os.stat_result) -> None:
    if _is_reparse_or_symlink(status):
        raise RuntimeError("session_key_unsafe_path")
    if not stat.S_ISREG(status.st_mode):
        raise RuntimeError("session_key_not_regular")
    if status.st_nlink != 1:
        raise RuntimeError("session_key_hardlink")


def _is_reparse_or_symlink(status: os.stat_result) -> bool:
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return stat.S_ISLNK(status.st_mode) or bool(
        getattr(status, "st_file_attributes", 0) & reparse_flag
    )


def _identity(status: os.stat_result):
    return status.st_dev, status.st_ino


if os.name == "nt":
    import ctypes
    from ctypes import wintypes
    import msvcrt

    _GENERIC_READ = 0x80000000
    _GENERIC_WRITE = 0x40000000
    _DELETE = 0x00010000
    _READ_CONTROL = 0x00020000
    _WRITE_DAC = 0x00040000
    _FILE_READ_ATTRIBUTES = 0x0080
    _FILE_WRITE_ATTRIBUTES = 0x0100
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _CREATE_NEW = 1
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
    _FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    _ERROR_FILE_NOT_FOUND = 2
    _ERROR_PATH_NOT_FOUND = 3
    _ERROR_SHARING_VIOLATION = 32
    _ERROR_FILE_EXISTS = 80
    _ERROR_ALREADY_EXISTS = 183
    _FILE_DISPOSITION_INFO_CLASS = 4
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
    _SDDL_REVISION_1 = 1
    _SE_FILE_OBJECT = 1
    _DACL_SECURITY_INFORMATION = 0x00000004
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _SE_DACL_PROTECTED = 0x1000
    _ACL_SIZE_INFORMATION_CLASS = 2
    _SECURE_KEY_SDDL = "D:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;OW)"

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    class _FileDispositionInfo(ctypes.Structure):
        _fields_ = [("DeleteFile", wintypes.BOOL)]

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        ]

    class _AclSizeInformation(ctypes.Structure):
        _fields_ = [
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _create_file = _kernel32.CreateFileW
    _create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _create_file.restype = wintypes.HANDLE
    _close_handle = _kernel32.CloseHandle
    _close_handle.argtypes = [wintypes.HANDLE]
    _close_handle.restype = wintypes.BOOL
    _get_file_information = _kernel32.GetFileInformationByHandle
    _get_file_information.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    ]
    _get_file_information.restype = wintypes.BOOL
    _set_file_information = _kernel32.SetFileInformationByHandle
    _set_file_information.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _set_file_information.restype = wintypes.BOOL
    _get_final_path_name = _kernel32.GetFinalPathNameByHandleW
    _get_final_path_name.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    _get_final_path_name.restype = wintypes.DWORD
    _local_free = _kernel32.LocalFree
    _local_free.argtypes = [wintypes.HLOCAL]
    _local_free.restype = wintypes.HLOCAL
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _convert_sddl = (
        _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    )
    _convert_sddl.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _convert_sddl.restype = wintypes.BOOL
    _get_security_descriptor_dacl = _advapi32.GetSecurityDescriptorDacl
    _get_security_descriptor_dacl.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.BOOL),
    ]
    _get_security_descriptor_dacl.restype = wintypes.BOOL
    _set_security_info = _advapi32.SetSecurityInfo
    _set_security_info.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
    ]
    _set_security_info.restype = wintypes.DWORD
    _get_security_info = _advapi32.GetSecurityInfo
    _get_security_info.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    ]
    _get_security_info.restype = wintypes.DWORD
    _get_security_descriptor_control = (
        _advapi32.GetSecurityDescriptorControl
    )
    _get_security_descriptor_control.argtypes = [
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    _get_security_descriptor_control.restype = wintypes.BOOL
    _get_acl_information = _advapi32.GetAclInformation
    _get_acl_information.argtypes = [
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.c_int,
    ]
    _get_acl_information.restype = wintypes.BOOL


def _load_or_create_windows(path: Path) -> str:
    ancestor_chain = _windows_open_ancestor_chain(path.parent)
    try:
        pinned_parent_handle = ancestor_chain[-1][1]
        last_transient_error = None
        for _attempt in range(_READ_RETRIES):
            descriptor = -1
            target_handle = _INVALID_HANDLE_VALUE
            created = False
            try:
                try:
                    target_handle = _windows_open_target(path, create=False)
                except OSError as error:
                    if error.winerror not in {
                        _ERROR_FILE_NOT_FOUND,
                        _ERROR_PATH_NOT_FOUND,
                        _ERROR_SHARING_VIOLATION,
                    }:
                        raise
                    if error.winerror == _ERROR_SHARING_VIOLATION:
                        time.sleep(_READ_RETRY_SECONDS)
                        continue
                    try:
                        target_handle = _windows_open_target(
                            path,
                            create=True,
                        )
                        created = True
                    except OSError as create_error:
                        if create_error.winerror not in {
                            _ERROR_FILE_EXISTS,
                            _ERROR_ALREADY_EXISTS,
                            _ERROR_SHARING_VIOLATION,
                        }:
                            raise
                        time.sleep(_READ_RETRY_SECONDS)
                        continue
                _verify_windows_target_parent(
                    target_handle,
                    pinned_parent_handle,
                    path.name,
                )
                target_info = _windows_information(target_handle)
                _validate_windows_target_info(target_info)
                target_identity = _windows_identity(target_info)
                descriptor = msvcrt.open_osfhandle(
                    target_handle,
                    os.O_RDWR | os.O_BINARY,
                )
                target_handle = _INVALID_HANDLE_VALUE
                _tighten_descriptor_permissions(descriptor)
                if created:
                    _write_new_key_to_descriptor(descriptor)
                value = _read_key_from_descriptor(descriptor)
                _verify_windows_target(path, target_identity)
                _verify_windows_ancestor_chain(ancestor_chain)
                return value
            except RuntimeError as error:
                if created:
                    _cleanup_windows_created(
                        descriptor,
                        target_handle,
                    )
                if str(error) not in _TRANSIENT_CONTENT_ERRORS:
                    raise
                last_transient_error = error
                time.sleep(_READ_RETRY_SECONDS)
            except BaseException:
                if created:
                    _cleanup_windows_created(
                        descriptor,
                        target_handle,
                    )
                raise
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                if target_handle != _INVALID_HANDLE_VALUE:
                    _close_handle(target_handle)
                    target_handle = _INVALID_HANDLE_VALUE
        if last_transient_error is not None:
            raise last_transient_error
        raise RuntimeError("session_key_creation_race")
    finally:
        _close_windows_ancestor_chain(ancestor_chain)


def _windows_open_ancestor_chain(path: Path):
    chain = []
    try:
        current = Path(path.anchor)
        for component in (current, *path.parts[1:]):
            if not isinstance(component, Path):
                current = current / component
            status = os.lstat(current)
            _validate_parent_status(status)
            try:
                handle = _windows_open_parent(current)
            except PermissionError as error:
                if error.winerror != 5:
                    raise
                chain.append((current, None, status.st_ino))
                continue
            info = _windows_information(handle)
            file_index = _windows_file_index(info)
            if file_index != status.st_ino:
                _close_handle(handle)
                raise RuntimeError("session_key_unsafe_path")
            chain.append(
                (current, handle, file_index)
            )
        if not chain or chain[-1][1] is None:
            raise RuntimeError("session_key_unsafe_path")
        _verify_windows_final_path(chain[-1][1], path)
        return chain
    except BaseException:
        _close_windows_ancestor_chain(chain)
        raise


def _close_windows_ancestor_chain(chain) -> None:
    for _path, handle, _identity_value in reversed(chain):
        if handle is not None:
            _close_handle(handle)


def _verify_windows_ancestor_chain(chain) -> None:
    for path, _handle, expected_identity in chain:
        status = os.lstat(path)
        _validate_parent_status(status)
        if status.st_ino != expected_identity:
            raise RuntimeError("session_key_unsafe_path")
        if _handle is None:
            continue
        handle = _windows_open_parent(path)
        try:
            current_identity = _windows_file_index(
                _windows_information(handle)
            )
            if current_identity != expected_identity:
                raise RuntimeError("session_key_unsafe_path")
        finally:
            _close_handle(handle)
    _verify_windows_final_path(chain[-1][1], chain[-1][0])


def _verify_windows_final_path(handle, expected_path: Path) -> None:
    actual = _windows_final_path(handle)
    expected = os.path.normcase(os.path.abspath(expected_path))
    if actual != expected:
        raise RuntimeError("session_key_unsafe_path")


def _windows_final_path(handle) -> str:
    size = _get_final_path_name(handle, None, 0, 0)
    if not size:
        error = ctypes.get_last_error()
        raise ctypes.WinError(error)
    buffer = ctypes.create_unicode_buffer(size + 1)
    result = _get_final_path_name(handle, buffer, len(buffer), 0)
    if not result or result >= len(buffer):
        error = ctypes.get_last_error()
        raise ctypes.WinError(error)
    actual = buffer.value
    if actual.startswith("\\\\?\\UNC\\"):
        actual = "\\\\" + actual[8:]
    elif actual.startswith("\\\\?\\"):
        actual = actual[4:]
    return os.path.normcase(os.path.abspath(actual))


def _verify_windows_target_parent(
    target_handle,
    parent_handle,
    expected_name: str,
) -> None:
    target_path = _windows_final_path(target_handle)
    parent_path = _windows_final_path(parent_handle)
    if (
        os.path.dirname(target_path) != parent_path
        or os.path.basename(target_path) != os.path.normcase(expected_name)
    ):
        raise RuntimeError("session_key_unsafe_path")


def _windows_open_parent(path: Path):
    handle = _windows_create_file(
        path,
        0,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS,
    )
    info = _windows_information(handle)
    if (
        info.dwFileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT
        or not info.dwFileAttributes & _FILE_ATTRIBUTE_DIRECTORY
    ):
        _close_handle(handle)
        raise RuntimeError("session_key_unsafe_path")
    return handle


def _set_windows_descriptor_permissions(descriptor: int) -> None:
    security_descriptor = _windows_secure_security_descriptor()
    try:
        dacl_present = wintypes.BOOL()
        dacl_defaulted = wintypes.BOOL()
        dacl = wintypes.LPVOID()
        if not _get_security_descriptor_dacl(
            security_descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ):
            error = ctypes.get_last_error()
            raise ctypes.WinError(error)
        if not dacl_present.value or not dacl:
            raise OSError("session_key_dacl_missing")
        result = _set_security_info(
            msvcrt.get_osfhandle(descriptor),
            _SE_FILE_OBJECT,
            (
                _DACL_SECURITY_INFORMATION
                | _PROTECTED_DACL_SECURITY_INFORMATION
            ),
            None,
            None,
            dacl,
            None,
        )
        if result:
            raise ctypes.WinError(result)
    finally:
        _local_free(security_descriptor)


def _windows_secure_security_descriptor():
    security_descriptor = wintypes.LPVOID()
    descriptor_size = wintypes.DWORD()
    if not _convert_sddl(
        _SECURE_KEY_SDDL,
        _SDDL_REVISION_1,
        ctypes.byref(security_descriptor),
        ctypes.byref(descriptor_size),
    ):
        error = ctypes.get_last_error()
        raise ctypes.WinError(error)
    return security_descriptor


def _windows_descriptor_permissions_are_secure(descriptor: int) -> bool:
    dacl = wintypes.LPVOID()
    security_descriptor = wintypes.LPVOID()
    result = _get_security_info(
        msvcrt.get_osfhandle(descriptor),
        _SE_FILE_OBJECT,
        _DACL_SECURITY_INFORMATION,
        None,
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(security_descriptor),
    )
    if result:
        raise ctypes.WinError(result)
    try:
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not _get_security_descriptor_control(
            security_descriptor,
            ctypes.byref(control),
            ctypes.byref(revision),
        ):
            error = ctypes.get_last_error()
            raise ctypes.WinError(error)
        acl_info = _AclSizeInformation()
        if not dacl or not _get_acl_information(
            dacl,
            ctypes.byref(acl_info),
            ctypes.sizeof(acl_info),
            _ACL_SIZE_INFORMATION_CLASS,
        ):
            error = ctypes.get_last_error()
            raise ctypes.WinError(error)
        return bool(control.value & _SE_DACL_PROTECTED) and (
            acl_info.AceCount == 3
        )
    finally:
        _local_free(security_descriptor)


def _windows_open_target(path: Path, *, create: bool):
    security_descriptor = None
    security_attributes = None
    if create:
        security_descriptor = _windows_secure_security_descriptor()
        security_attributes = _SecurityAttributes(
            ctypes.sizeof(_SecurityAttributes),
            security_descriptor,
            False,
        )
    try:
        return _windows_create_file(
            path,
            (
                _GENERIC_READ
                | _GENERIC_WRITE
                | _DELETE
                | _READ_CONTROL
                | _WRITE_DAC
                | _FILE_READ_ATTRIBUTES
                | _FILE_WRITE_ATTRIBUTES
            ),
            _FILE_SHARE_READ,
            _CREATE_NEW if create else _OPEN_EXISTING,
            (
                _FILE_ATTRIBUTE_NORMAL
                | _FILE_FLAG_BACKUP_SEMANTICS
                | _FILE_FLAG_OPEN_REPARSE_POINT
            ),
            (
                ctypes.byref(security_attributes)
                if security_attributes is not None
                else None
            ),
        )
    finally:
        if security_descriptor is not None:
            _local_free(security_descriptor)


def _windows_create_file(
    path: Path,
    access: int,
    share: int,
    disposition: int,
    flags: int,
    security_attributes=None,
):
    handle = _create_file(
        str(path),
        access,
        share,
        security_attributes,
        disposition,
        flags,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        error = ctypes.get_last_error()
        raise ctypes.WinError(error)
    return handle


def _windows_information(handle):
    information = _ByHandleFileInformation()
    if not _get_file_information(handle, ctypes.byref(information)):
        error = ctypes.get_last_error()
        raise ctypes.WinError(error)
    return information


def _validate_windows_target_info(info) -> None:
    if info.dwFileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
        raise RuntimeError("session_key_unsafe_path")
    if info.dwFileAttributes & _FILE_ATTRIBUTE_DIRECTORY:
        raise RuntimeError("session_key_not_regular")
    if info.nNumberOfLinks != 1:
        raise RuntimeError("session_key_hardlink")


def _windows_identity(info):
    return (
        info.dwVolumeSerialNumber,
        info.nFileIndexHigh,
        info.nFileIndexLow,
    )


def _windows_file_index(info) -> int:
    return (info.nFileIndexHigh << 32) | info.nFileIndexLow


def _verify_windows_target(path: Path, expected_identity) -> None:
    handle = _windows_create_file(
        path,
        _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
    )
    try:
        info = _windows_information(handle)
        _validate_windows_target_info(info)
        if _windows_identity(info) != expected_identity:
            raise RuntimeError("session_key_unsafe_path")
    finally:
        _close_handle(handle)


def _cleanup_windows_created(
    descriptor: int,
    raw_handle=_INVALID_HANDLE_VALUE,
) -> None:
    if descriptor < 0 and raw_handle == _INVALID_HANDLE_VALUE:
        return
    handle = (
        msvcrt.get_osfhandle(descriptor)
        if descriptor >= 0
        else raw_handle
    )
    disposition = _FileDispositionInfo(True)
    if not _set_file_information(
        handle,
        _FILE_DISPOSITION_INFO_CLASS,
        ctypes.byref(disposition),
        ctypes.sizeof(disposition),
    ):
        error = ctypes.get_last_error()
        raise RuntimeError("session_key_cleanup_failed") from ctypes.WinError(
            error
        )


__all__ = ["load_or_create_session_key"]
