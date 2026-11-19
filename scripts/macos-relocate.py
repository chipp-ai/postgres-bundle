#!/usr/bin/env python3
"""Make a macOS PostgreSQL install tree relocatable, or check that it is.

    macos-relocate.py fix   <root>
    macos-relocate.py check <root>

`fix` walks every Mach-O file under <root>/bin and <root>/lib. Each
dependency outside the system locations (/usr/lib, /System) is copied into
<root>/lib (following its own dependencies, e.g. libssl -> libcrypto,
libicuuc -> libicudata) and the load command is rewritten to an
@loader_path-relative path:

    bin/*          -> @loader_path/../lib/<name>
    lib/*          -> @loader_path/<name>
    lib/<sub>/*    -> @loader_path/../<name> (and so on, by depth)

Dylib install names (LC_ID_DYLIB) become @loader_path/<name>, absolute
LC_RPATH entries are dropped, and every Mach-O in the tree is ad-hoc
re-signed, since install_name_tool invalidates the signature and arm64
macOS kills binaries with a broken one.

`check` fails (exit 1) if any Mach-O references a path that is not under
/usr/lib or /System and not an @loader_path/@rpath/@executable_path path
that resolves to a file inside <root>, has an absolute LC_RPATH, or has a
signature that does not verify, or was built for a newer macOS than
$MACOS_MIN (default 12.0, the desktop app's minimumSystemVersion).
"""

import os
import shutil
import subprocess
import sys

SYSTEM_PREFIXES = ("/usr/lib/", "/System/")


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def is_macho(path):
    if os.path.islink(path) or not os.path.isfile(path):
        return False
    with open(path, "rb") as f:
        magic = f.read(4)
    return magic in (
        b"\xcf\xfa\xed\xfe",  # MH_MAGIC_64 (little endian)
        b"\xce\xfa\xed\xfe",  # MH_MAGIC
        b"\xca\xfe\xba\xbe",  # FAT_MAGIC (universal)
    )


def macho_files(root):
    out = []
    for sub in ("bin", "lib"):
        for dirpath, _dirs, files in os.walk(os.path.join(root, sub)):
            for name in files:
                p = os.path.join(dirpath, name)
                if is_macho(p):
                    out.append(p)
    return sorted(out)


def install_id(path):
    lines = run("otool", "-D", path).splitlines()
    return lines[1].strip() if len(lines) > 1 else None


def deps(path):
    """Load commands of `path`, excluding its own install name."""
    own = install_id(path)
    result = []
    for line in run("otool", "-L", path).splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        name = line.split(" (compatibility")[0].strip()
        if name == own:
            own = None  # skip only the first (the LC_ID_DYLIB line)
            continue
        result.append(name)
    return result


def rpaths(path):
    out, lines = [], run("otool", "-l", path).splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "cmd LC_RPATH":
            for follow in lines[i + 1 : i + 4]:
                follow = follow.strip()
                if follow.startswith("path "):
                    out.append(follow[5:].split(" (offset")[0])
    return out


def is_system(dep):
    return dep.startswith(SYSTEM_PREFIXES)


def resolve_relative(dep, path, origin_dir=None):
    """Resolve an @loader_path/@executable_path/@rpath dep of `path`.

    origin_dir: where `path` was copied from (for libs pulled in from
    Homebrew, whose own @loader_path refs point at their source dir).
    """
    base = origin_dir or os.path.dirname(path)
    if dep.startswith("@loader_path/") or dep.startswith("@executable_path/"):
        return os.path.normpath(os.path.join(base, dep.split("/", 1)[1]))
    if dep.startswith("@rpath/"):
        rest = dep[len("@rpath/") :]
        for rp in rpaths(path):
            if rp.startswith("@loader_path"):
                rp = base + rp[len("@loader_path") :]
            elif rp.startswith("@executable_path"):
                rp = base + rp[len("@executable_path") :]
            cand = os.path.normpath(os.path.join(rp, rest))
            if os.path.exists(cand):
                return cand
    return None


def loader_ref(path, libdir, name):
    rel = os.path.relpath(os.path.join(libdir, name), os.path.dirname(path))
    return "@loader_path/" + rel


def fix(root):
    root = os.path.abspath(root)
    libdir = os.path.join(root, "lib")
    origins = {}  # copied file in tree -> directory it was copied from
    queue = macho_files(root)
    seen = set()
    while queue:
        path = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        os.chmod(path, os.stat(path).st_mode | 0o200)
        changes = []
        for dep in deps(path):
            if is_system(dep):
                continue
            name = os.path.basename(dep)
            if dep.startswith("@"):
                src = resolve_relative(dep, path, origins.get(path))
                if src is None:
                    sys.exit(f"{path}: cannot resolve {dep}")
            elif "/" not in dep:
                # Bare leaf name: resolved next to the file at link time.
                src = os.path.join(origins.get(path, os.path.dirname(path)), dep)
            else:
                src = dep
            target = os.path.join(libdir, name)
            if os.path.realpath(src) != os.path.realpath(target):
                if not os.path.exists(target):
                    if not os.path.exists(src):
                        sys.exit(f"{path}: dependency {dep} not found")
                    shutil.copy2(os.path.realpath(src), target)
                    os.chmod(target, 0o755)
                    origins[target] = os.path.dirname(os.path.realpath(src))
                    queue.append(target)
                    print(f"copied {src} -> lib/{name}")
            new = loader_ref(path, libdir, name)
            if new != dep:
                changes += ["-change", dep, new]
        own = install_id(path)
        if own is not None:
            want = "@loader_path/" + os.path.basename(path)
            if own != want:
                changes += ["-id", want]
        for rp in rpaths(path):
            if not rp.startswith("@"):
                changes += ["-delete_rpath", rp]
        if changes:
            run("install_name_tool", *changes, path)
            print(f"rewrote {os.path.relpath(path, root)}")
    for path in macho_files(root):
        run("codesign", "--force", "--sign", "-", path)
    print(f"signed {len(macho_files(root))} Mach-O files")


def version_tuple(v):
    return tuple(int(x) for x in v.split("."))


def min_os(path):
    """Deployment target(s) recorded in the binary (one per arch slice)."""
    out, lines = [], run("otool", "-l", path).splitlines()
    for i, line in enumerate(lines):
        cmd = line.strip()
        if cmd in ("cmd LC_BUILD_VERSION", "cmd LC_VERSION_MIN_MACOSX"):
            for follow in lines[i + 1 : i + 6]:
                parts = follow.split()
                if parts and parts[0] in ("minos", "version"):
                    out.append(parts[1])
                    break
    return out


def check(root):
    root = os.path.abspath(root)
    problems = []
    macos_min = os.environ.get("MACOS_MIN", "12.0")
    files = macho_files(root)
    for path in files:
        rel = os.path.relpath(path, root)
        for dep in deps(path):
            if is_system(dep):
                continue
            if not dep.startswith("@"):
                problems.append(f"{rel}: absolute non-system dependency {dep}")
                continue
            resolved = resolve_relative(dep, path)
            if resolved is None or not os.path.exists(resolved):
                problems.append(f"{rel}: {dep} does not resolve")
            elif not os.path.realpath(resolved).startswith(root + os.sep):
                problems.append(f"{rel}: {dep} resolves outside the tree")
        own = install_id(path)
        if own and not own.startswith("@"):
            problems.append(f"{rel}: absolute install name {own}")
        for rp in rpaths(path):
            if not rp.startswith("@"):
                problems.append(f"{rel}: absolute LC_RPATH {rp}")
        for v in min_os(path):
            if version_tuple(v) > version_tuple(macos_min):
                problems.append(f"{rel}: built for macOS {v}, newer than {macos_min}")
        r = subprocess.run(["codesign", "--verify", path], capture_output=True, text=True)
        if r.returncode != 0:
            problems.append(f"{rel}: codesign --verify failed: {r.stderr.strip()}")
    for p in problems:
        print("FAIL " + p)
    print(f"checked {len(files)} Mach-O files, {len(problems)} problems")
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("fix", "check"):
        sys.exit(__doc__)
    {"fix": fix, "check": check}[sys.argv[1]](sys.argv[2])
