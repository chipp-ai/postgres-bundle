#!/usr/bin/env python3
"""Make a Linux PostgreSQL install tree relocatable, or check that it is.

    linux-relocate.py fix   <root>
    linux-relocate.py check <root>

As built on the runner, the binaries load ICU, OpenSSL, libxml2 and the
rest from the runner's /usr/lib, so the archive only runs where those exact
sonames exist. On Arch (ICU 78) `initdb` failed with "libicuuc.so.74:
cannot open shared object file".

`fix` walks every ELF file under <root>/bin and <root>/lib, copies each
shared library they resolve to (transitively, as ldd reports it) into
<root>/lib unless it is part of the C runtime every Linux system has (glibc,
the dynamic loader, libgcc_s, libstdc++), and sets each ELF's RUNPATH to an
$ORIGIN-relative path:

    bin/*          -> $ORIGIN/../lib
    lib/*          -> $ORIGIN
    lib/<sub>/*    -> $ORIGIN/.. (and so on, by depth)

`check` fails (exit 1) if any ELF has a RUNPATH/RPATH that is not
$ORIGIN-relative, or depends on a library that is neither in the C runtime
allow list nor present in <root>/lib. It reads the dynamic section, not
ldd, so it does not depend on what the build host has installed.
"""

import os
import re
import shutil
import subprocess
import sys

# Sonames every glibc-based distribution ships, ABI-compatible forward.
# Everything else is bundled.
RUNTIME = re.compile(
    r"^(linux-vdso|linux-gate|ld-linux[^/]*|libc|libm|libdl|libpthread|librt|"
    r"libresolv|libutil|libgcc_s|libstdc\+\+)\.so"
)


def run(*args, env=None):
    return subprocess.run(args, check=True, capture_output=True, text=True, env=env).stdout


def is_elf(path):
    if os.path.islink(path) or not os.path.isfile(path):
        return False
    with open(path, "rb") as f:
        return f.read(4) == b"\x7fELF"


def elf_files(root):
    out = []
    for sub in ("bin", "lib"):
        for dirpath, _dirs, files in os.walk(os.path.join(root, sub)):
            for name in files:
                p = os.path.join(dirpath, name)
                if is_elf(p):
                    out.append(p)
    return sorted(out)


def needed(path):
    """DT_NEEDED sonames, from the dynamic section."""
    return re.findall(r"\(NEEDED\)\s+Shared library: \[([^\]]+)\]", run("readelf", "-d", path))


def runpaths(path):
    return re.findall(r"\((?:RUNPATH|RPATH)\)\s+Library (?:runpath|rpath): \[([^\]]*)\]", run("readelf", "-d", path))


def resolved(path, lib):
    """soname -> absolute path, as the build host's loader resolves it.

    The tree's own lib/ is searched first: the binaries' RUNPATH still
    names the build's install prefix, which may not exist any more."""
    env = dict(os.environ, LD_LIBRARY_PATH=lib)
    out = {}
    for line in run("ldd", path, env=env).splitlines():
        m = re.match(r"\s*(\S+) => (/\S+)", line)
        if m:
            out[m.group(1)] = m.group(2)
        elif "=> not found" in line:
            raise SystemExit(f"{path}: {line.strip()}")
    return out


def origin_path(root, path):
    lib = os.path.join(root, "lib")
    rel = os.path.relpath(lib, os.path.dirname(path))
    return "$ORIGIN" if rel == "." else f"$ORIGIN/{rel}"


def fix(root):
    lib = os.path.join(root, "lib")
    copied = set()
    pending = elf_files(root)
    while pending:
        path = pending.pop()
        for soname, target in resolved(path, lib).items():
            if RUNTIME.match(soname) or target.startswith(os.path.abspath(root) + "/"):
                continue
            dest = os.path.join(lib, soname)
            if soname not in copied and not os.path.exists(dest):
                shutil.copy2(os.path.realpath(target), dest)
                os.chmod(dest, 0o755)
                copied.add(soname)
                pending.append(dest)
    for path in elf_files(root):
        run("patchelf", "--set-rpath", origin_path(root, path), path)
    print(f"bundled {len(copied)} libraries: {', '.join(sorted(copied)) or 'none'}")


def check(root):
    lib = os.path.join(root, "lib")
    problems = []
    for path in elf_files(root):
        rel = os.path.relpath(path, root)
        for rp in runpaths(path):
            for entry in rp.split(":"):
                if entry and not entry.startswith("$ORIGIN"):
                    problems.append(f"{rel}: runpath entry {entry!r} is not $ORIGIN-relative")
        for soname in needed(path):
            if RUNTIME.match(soname):
                continue
            if not os.path.exists(os.path.join(lib, soname)):
                problems.append(f"{rel}: needs {soname}, which is not in lib/ or the C runtime")
    for p in problems:
        print(p, file=sys.stderr)
    if problems:
        raise SystemExit(f"{len(problems)} relocation problems")
    print(f"relocatable: {len(elf_files(root))} ELF files checked")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("fix", "check"):
        raise SystemExit(__doc__)
    {"fix": fix, "check": check}[sys.argv[1]](sys.argv[2])
