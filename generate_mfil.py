#!/usr/bin/env python3
"""
generate_mfil.py - build a WoW.mfil manifest for the 3.3.5 client's native
streaming downloader, and lay the streamed files out for an HTTP server.

How the client uses it:
  At launch the client looks for WoW.mfil in its root. If it is there, streaming
  starts and any missing data file is pulled over HTTP from the `source` URL named
  in the manifest.

  The manifest is plain text, one `key=value` per line. A handful of keys are
  reserved (version, source, manifest, transportmanifest, isTrial, bgpreloadsleep,
  sourcemanifest). Any other key is a file entry, with the value `path;size;md5;flags`.
  With `source=http://example.com/streaming`, the client fetches each file from
  http://example.com/streaming/<path> using HTTP range requests, so the layout under
  the source has to mirror the in-game relative paths.

  MPQ archives are streamed whole, like any other file. The client downloads byte
  ranges of the archive as it needs them.

Usage:
  python generate_mfil.py CONTENT_DIR --url URL [options]

  CONTENT_DIR   directory whose tree mirrors the client-relative layout, e.g.
                CONTENT_DIR/Data/enUS/patch-3.MPQ -> in-game Data/enUS/patch-3.MPQ

It writes two files: a small WoW.mfil client stub (current directory) that redirects to a
full streaming.mfil hosted on the server, and that streaming.mfil staged in --serve-root.
The client gets WoW.mfil, follows the redirect, and reads streaming.mfil from the server.

Options:
  --url URL        source base URL (required). Must be http://, the client's
                   streaming transport rejects https://.
  --serve-root DIR stage every streamed file plus streaming.mfil here, then host this
                   directory as the streaming root (required)
  --include GLOB   only include files whose relpath matches this glob (repeatable)
  --exclude GLOB   skip files whose relpath matches this glob (repeatable)

Example:
  python generate_mfil.py ./content --url http://example.com/streaming --serve-root ./serve
"""

import argparse
import fnmatch
import hashlib
import os
import shutil
import sys


RESERVED = {
    "version", "source", "sourcemanifest", "manifest",
    "transportmanifest", "istrial", "bgpreloadsleep",
}

# The client only looks for a manifest by this exact name in its root.
MANIFEST_NAME = "WoW.mfil"
# The full server-hosted manifest the client stub redirects to.
SERVER_MANIFEST_NAME = "streaming.mfil"


def md5_file(path, chunk=1 << 20):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def glob_match(rel, globs):
    return any(fnmatch.fnmatch(rel, g) or fnmatch.fnmatch(rel.lower(), g.lower())
               for g in globs)


def stage_copy(serve_root, rel, src):
    dst = os.path.join(serve_root, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if os.path.exists(dst) and os.path.getsize(dst) == os.path.getsize(src):
        return
    shutil.copy2(src, dst)


def iter_loose(root):
    """(relpath, abspath) for every file under root, with forward-slash relpaths."""
    for dirpath, _dirs, names in os.walk(root):
        for name in sorted(names):
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            yield rel, full


def main(argv):
    ap = argparse.ArgumentParser(description="Generate a WoW.mfil streaming manifest.")
    ap.add_argument("content_dir")
    ap.add_argument("--url", required=True,
                    help="source base URL, e.g. http://example.com/streaming (http:// only)")
    ap.add_argument("--serve-root", required=True,
                    help="stage every streamed file plus streaming.mfil here, then host this "
                         "directory as the streaming root")
    ap.add_argument("--include", action="append", default=[])
    ap.add_argument("--exclude", action="append", default=[])
    # Background-preload throttle in ms. The client clamps it to [10, 1000] (default 100
    # when absent), so 10 means download as fast as the client allows.
    ap.add_argument("--bgpreloadsleep", type=int, default=10)
    args = ap.parse_args(argv)

    content = os.path.abspath(args.content_dir)
    if not os.path.isdir(content):
        ap.error("content_dir does not exist: %s" % content)
    serve_root = os.path.abspath(args.serve_root)

    source = args.url.rstrip("/")  # client appends "/" + relpath itself

    def wanted(rel):
        if rel.lower() in RESERVED:
            print("warning: skipping reserved-name file %r" % rel, file=sys.stderr)
            return False
        if args.include and not glob_match(rel, args.include):
            return False
        if args.exclude and glob_match(rel, args.exclude):
            return False
        return True

    bgsleep = max(10, min(1000, args.bgpreloadsleep))  # client clamps to [10, 1000]
    lines = ["version=1", "isTrial=0", "bgpreloadsleep=%d" % bgsleep, "source=%s" % source]
    seen = {}          # lowercased relpath -> origin, for collision reporting
    count = 0
    total = 0

    def emit(rel, size, digest):
        nonlocal count, total
        lines.append("%s=%s;%d;%s;0" % (rel, rel, size, digest))
        count += 1
        total += size

    for rel, full in iter_loose(content):
        if not wanted(rel):
            continue
        if rel.lower() in seen:
            print("warning: duplicate %r" % rel, file=sys.stderr)
            continue
        size = os.path.getsize(full)
        # The client reads a size-0 entry as a directory (it calls CreateDirectory on the
        # path), so a 0-byte file would turn into a phantom folder. Skip it.
        if size == 0:
            print("warning: skipping 0-byte file %r (size 0 reads as a directory)" % rel,
                  file=sys.stderr)
            continue
        seen[rel.lower()] = "loose"
        digest = md5_file(full)
        stage_copy(serve_root, rel, full)
        emit(rel, size, digest)

    full_manifest = "\n".join(lines) + "\n"

    def write_file(path, text):
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", newline="\n", encoding="ascii") as f:
            f.write(text)
        return path

    # The full list goes in the serve root so it is reachable at <source>/streaming.mfil.
    # The client reads this one after following the stub's redirect.
    server_path = write_file(os.path.join(serve_root, SERVER_MANIFEST_NAME), full_manifest)
    manifest_url = "%s/%s" % (source, SERVER_MANIFEST_NAME)
    # Client stub: version first so the parser hands off, then the redirect. This is the
    # WoW.mfil that goes on the client.
    stub = "version=1\nmanifest=%s\n" % manifest_url
    stub_path = write_file(MANIFEST_NAME, stub)

    print("  server (hosted) : %s" % server_path)
    print("                    serve at %s" % manifest_url)
    print("  client stub     : %s  (redirects to the above)" % stub_path)
    print("  %d files, %.1f MiB total" % (count, total / (1024 * 1024)))
    print("  source = %s" % source)
    print("  staged files under %s" % serve_root)
    print()
    print("Next steps:")
    print("  1. Host the serve root so %s and <source>/<relpath> resolve" % manifest_url)
    print("     with HTTP range support:  python serve_streaming.py %s" % serve_root)
    print("  2. Put the client stub %s in the client root (next to Wow.exe)." % stub_path)
    print("  3. Launch the client. It follows the redirect and reads the server manifest.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
