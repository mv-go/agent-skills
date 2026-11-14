#!/usr/bin/env python3
"""Read-only disk allocation observations; no cleanup actions.

Stdout is capped at 20 rows; --output JSON lists every immediate child."""
import argparse
import json
import os
import shutil
import stat
import subprocess
import sys

MAX_ROWS = 20
MAX_ERRORS = 10


def inventory(path, children=False, timeout=60):
    result = {"path": path, "status": "complete", "allocated_bytes": None,
              "filesystem": None, "children": [], "errors": []}

    def error(message):
        result["status"] = "partial"
        if len(result["errors"]) < MAX_ERRORS:
            result["errors"].append(message[:500])

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
            else:
                error("Root is neither a regular file nor a directory")
            return result
    except OSError as exc:
        error(str(exc))
        return result

    # du does not follow symlinks by default. -x confines traversal to a filesystem.
    # -d 1 provides immediate directory observations in the same traversal.
    command = ["du", "-kx", "-d", "1" if children else "0", path]
    try:
        proc = subprocess.run(command, capture_output=True, text=True, errors="replace",
                              timeout=timeout, env={**os.environ, "LC_ALL": "C"})
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
    if children:
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    try:
                        item = entry.stat(follow_symlinks=False)
                        if stat.S_ISREG(item.st_mode):
                            result["children"].append({"path": entry.path, "kind": "file",
                                                       "allocated_bytes": item.st_blocks * 512})
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
    parser.add_argument("--timeout", type=float, default=60, help="du walk timeout per --path root in seconds (default 60)")
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= 3600:
        parser.error("--timeout must be greater than zero and at most 3600 seconds")
    # Reserve the destination first. O_EXCL also rejects existing symlinks and prevents clobbering.
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
                "note": "Independent observations; do not sum overlapping paths or infer physical savings (clones and shared blocks may overlap)."}
    if out:
        try:
            with out:
                json.dump(document, out, ensure_ascii=True, indent=2)
                out.write("\n")
        except OSError as exc:
            print("Cannot write output: %s" % exc, file=sys.stderr)
            return 2
    for item in observations[:MAX_ROWS]:
        print("%s  allocated_bytes=%s  %s" % (item["status"], item["allocated_bytes"], item["path"]))
        for child in item["children"][:MAX_ROWS]:
            print("  %s  %s  %s" % (child["allocated_bytes"], child["kind"], child["path"]))
        if len(item["children"]) > MAX_ROWS:
            print("  ... %d more children omitted from stdout" % (len(item["children"]) - MAX_ROWS))
        for message in item["errors"]:
            print("  error: %s" % message)
    if len(observations) > MAX_ROWS:
        print("... %d more observations omitted from stdout" % (len(observations) - MAX_ROWS))
    print(document["note"])
    return 1 if any(item["status"] != "complete" for item in observations) else 0


if __name__ == "__main__":
    sys.exit(main())
