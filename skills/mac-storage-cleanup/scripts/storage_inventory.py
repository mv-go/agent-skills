#!/usr/bin/env python3
"""Read-only, bounded-output disk allocation observations; no cleanup actions.

Stdout is capped at 20 rows; --output JSON lists every immediate child."""
import argparse
import ctypes
import json
import os
import shutil
import stat
import subprocess
import sys
import time

MAX_ROWS = 20
MAX_ERRORS = 10
SF_DATALESS = 0x40000000


class DownloadProtectionError(RuntimeError):
    pass


def protect_cloud_files():
    """Fail closed before path access and verify the policy survives exec.

    Values come from the macOS SDK's sys/resource.h and Apple TN3150. This
    changes only this process and its children, not the user's iCloud settings.
    """
    if sys.platform != "darwin":
        return {"status": "not_applicable", "platform": sys.platform}
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        setter, getter = libc.setiopolicy_np, libc.getiopolicy_np
        setter.argtypes, setter.restype = [ctypes.c_int] * 3, ctypes.c_int
        getter.argtypes, getter.restype = [ctypes.c_int] * 2, ctypes.c_int
        # IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES, IOPOL_SCOPE_PROCESS,
        # IOPOL_MATERIALIZE_DATALESS_FILES_OFF.
        if setter(3, 0, 1) != 0 or getter(3, 0) != 1:
            raise DownloadProtectionError("macOS refused the no-materialization policy")
        code = ("import ctypes; f=ctypes.CDLL(None).getiopolicy_np; "
                "f.argtypes=[ctypes.c_int,ctypes.c_int]; f.restype=ctypes.c_int; "
                "print(f(3,0))")
        child = subprocess.Popen([sys.executable, "-I", "-c", code],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            output, errors = child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.communicate()
            raise DownloadProtectionError("child policy verification timed out")
        if child.returncode or errors or output.strip() != "1":
            raise DownloadProtectionError("no-materialization policy did not survive exec")
    except (AttributeError, OSError) as exc:
        raise DownloadProtectionError("cannot establish macOS download protection: %s" % exc) from exc
    return {"status": "verified", "materialization": "off",
            "process_policy": 1, "exec_child_policy": 1}


def inventory(path, children=False, timeout=60):
    result = {"path": path, "status": "complete", "allocated_bytes": None,
              "apparent_bytes": None, "filesystem": None, "children": [], "errors": [],
              "download_protection": None}

    def error(message):
        result["status"] = "partial"
        if len(result["errors"]) < MAX_ERRORS:
            result["errors"].append(message[:500])

    try:
        result["download_protection"] = protect_cloud_files()
    except DownloadProtectionError as exc:
        result["status"] = "blocked"
        result["errors"].append(str(exc))
        return result

    try:
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode):
            error("Root is a symlink; skipped")
            return result
        usage = shutil.disk_usage(path)
        result["filesystem"] = {"total_bytes": usage.total, "free_bytes": usage.free,
                                "used_bytes": usage.used}
        if not stat.S_ISDIR(info.st_mode):
            if stat.S_ISREG(info.st_mode):
                result["allocated_bytes"] = info.st_blocks * 512
                result["apparent_bytes"] = info.st_size
                result["cloud_only"] = bool(getattr(info, "st_flags", 0) & SF_DATALESS)
            else:
                error("Root is neither a regular file nor a directory")
            return result
    except OSError as exc:
        error(str(exc))
        return result

    # du does not follow symlinks by default. -x confines traversal to a filesystem.
    # -d 1 provides immediate directory observations in the same traversal.
    command = ["du", "-kx", "-d", "1" if children else "0", path]
    deadline = time.monotonic() + timeout
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired("du", timeout)
        proc = subprocess.run(command, capture_output=True, text=True, errors="replace",
                              timeout=remaining, env={**os.environ, "LC_ALL": "C"})
        output = proc.stdout
        if proc.returncode:
            error("du failed (exit %s): %s" % (proc.returncode, proc.stderr.strip()))
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode(errors="replace")
        error("du timed out after %s seconds" % timeout)
    except OSError as exc:
        error("du could not start: %s" % exc)
        output = ""
    for line in output.splitlines():
        try:
            blocks, name = line.split("\t", 1)
            allocated = int(blocks) * 1024
            if allocated < 0:
                raise ValueError("negative allocation")
            if name == path:
                result["allocated_bytes"] = allocated
            elif children and os.path.dirname(name.rstrip("/")) == path:
                # Only report genuine immediate directories, never symlinks.
                entry_info = os.lstat(name)
                if stat.S_ISDIR(entry_info.st_mode):
                    result["children"].append({"path": name, "kind": "directory", "allocated_bytes": allocated})
        except (ValueError, OSError) as exc:
            error("Could not interpret a du row: %s" % exc)
    if result["allocated_bytes"] is None:
        error("No root allocation returned by du")

    # Logical-size metadata is collected under the same policy and within the
    # same wall-clock budget as allocated-size output. Never retry unprotected.
    apparent_flag = "-A" if sys.platform == "darwin" else "--apparent-size"
    apparent_command = ["du", apparent_flag, "-kx", "-d", "1" if children else "0", path]
    logical_children = {}
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired("du apparent-size metadata", timeout)
        proc = subprocess.run(apparent_command, capture_output=True, text=True, errors="replace",
                              timeout=remaining, env={**os.environ, "LC_ALL": "C"})
        if proc.returncode:
            error("apparent-size metadata incomplete (exit %s): %s" %
                  (proc.returncode, proc.stderr.strip()))
        for line in proc.stdout.splitlines():
            blocks, name = line.split("\t", 1)
            value = int(blocks) * 1024
            if value < 0:
                raise ValueError("negative apparent size")
            if name == path:
                result["apparent_bytes"] = value
            else:
                logical_children[name] = value
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        error("apparent-size metadata unavailable: %s" % exc)
    for row in result["children"]:
        row["apparent_bytes"] = logical_children.get(row["path"])

    if children:
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    try:
                        item = entry.stat(follow_symlinks=False)
                        if stat.S_ISREG(item.st_mode):
                            result["children"].append({"path": entry.path, "kind": "file",
                                                       "allocated_bytes": item.st_blocks * 512,
                                                       "apparent_bytes": item.st_size,
                                                       "cloud_only": bool(getattr(item, "st_flags", 0) & SF_DATALESS)})
                    except OSError as exc:
                        error("Cannot inspect %s: %s" % (entry.path, exc))
        except OSError as exc:
            error("Cannot list children: %s" % exc)
        result["children"].sort(key=lambda x: (-x["allocated_bytes"], x["path"]))
    return result


def absolute_path(value):
    expanded = os.path.expanduser(value)
    if not os.path.isabs(expanded):
        raise argparse.ArgumentTypeError("path must be absolute or start with ~/")
    return os.path.normpath(expanded)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", required=True, action="append", type=absolute_path,
                        help="absolute or home-expanded path; repeat for multiple observations")
    parser.add_argument("--children", action="store_true", help="include immediate files and directories")
    parser.add_argument("--output", type=absolute_path, help="create a new JSON file exclusively")
    parser.add_argument("--timeout", type=float, default=60, help="shared two-walk timeout per --path root in seconds (default 60)")
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= 3600:
        parser.error("--timeout must be greater than zero and at most 3600 seconds")
    try:
        protection = protect_cloud_files()
    except DownloadProtectionError as exc:
        print("Scan blocked before accessing requested paths: %s" % exc, file=sys.stderr)
        return 2
    # Reserve the destination after protection. O_EXCL also rejects existing symlinks and prevents clobbering.
    out = None
    if args.output:
        try:
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            out = os.fdopen(fd, "w", encoding="utf-8")
        except OSError as exc:
            print("Cannot create output: %s" % exc, file=sys.stderr)
            return 2
    observations = [inventory(p, args.children, args.timeout) for p in args.path]
    document = {"schema_version": 1, "units": "bytes", "observations": observations,
                "download_protection": protection,
                "note": "Independent observations; do not sum overlapping paths or infer physical savings (clones and shared blocks may overlap). Apparent size is metadata size, not iCloud quota; directory totals are rounded to KiB and can be incomplete when cloud metadata is unavailable. No unprotected retry is permitted."}
    if out:
        try:
            with out:
                json.dump(document, out, ensure_ascii=True, indent=2)
                out.write("\n")
        except OSError as exc:
            print("Cannot write output: %s" % exc, file=sys.stderr)
            return 2
    for item in observations[:MAX_ROWS]:
        print("%s  allocated_bytes=%s apparent_bytes=%s  %s" % (item["status"], item["allocated_bytes"], item["apparent_bytes"], item["path"]))
        for child in item["children"][:MAX_ROWS]:
            print("  local=%s apparent=%s  %s  %s" % (child["allocated_bytes"], child.get("apparent_bytes"), child["kind"], child["path"]))
        if len(item["children"]) > MAX_ROWS:
            print("  ... %d more children omitted from stdout" % (len(item["children"]) - MAX_ROWS))
        for message in item["errors"]:
            print("  error: %s" % message)
    if len(observations) > MAX_ROWS:
        print("... %d more observations omitted from stdout" % (len(observations) - MAX_ROWS))
    print(document["note"])
    if any(item["status"] == "blocked" for item in observations):
        return 2
    return 1 if any(item["status"] != "complete" for item in observations) else 0


if __name__ == "__main__":
    sys.exit(main())
