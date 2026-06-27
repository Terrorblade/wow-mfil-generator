# Client file streaming (WoW.mfil)

Tools for the 3.3.5 client's built-in streaming downloader. The client can pull missing data
files over HTTP at runtime instead of shipping them all up front. It is driven by a plain-text
manifest named `WoW.mfil` in the client root.

How it works:

- At launch the client runs `StartupStreaming("WoW.mfil")`. If the file exists, streaming
  starts while the client reads it. No DLL hook is needed to start it.
- The manifest is `key=value` lines. Reserved keys: `version`, `source`, `manifest`,
  `transportmanifest`, `sourcemanifest`, `isTrial`, `bgpreloadsleep`. Every other line is a
  file entry whose value is `path;size;md5;flags`.
- `source=http://example.com/streaming` tells the client where to fetch from. Each file is
  requested as `http://example.com/streaming/<relpath>` using HTTP byte-range requests, so the
  server layout must mirror the in-game relative paths. The transport is http only, https is
  rejected.

## Generate the manifest

```
python generate_mfil.py CONTENT_DIR \
    --url http://example.com/streaming \
    --serve-root ./serve
```

- `CONTENT_DIR` is a tree mirroring client-relative paths, e.g.
  `CONTENT_DIR/Data/enUS/patch-3.MPQ` -> in-game `Data/enUS/patch-3.MPQ`.
- `--serve-root` (required) stages a copy of every included file under `./serve/<relpath>` plus
  the server manifest, so that directory can be served directly as the streaming root.
- `--include` / `--exclude` take glob patterns (repeatable) to filter the tree.

This always writes a two-file layout: a tiny stub on the client that redirects to a full
manifest hosted on the server, which the client then reads. That lets you update the file list
server-side without re-touching clients. It produces:

- `./serve/streaming.mfil` - the **server-hosted** manifest (`version` + `source` + full list),
  staged in the serve root so it is reachable at `<source>/streaming.mfil`. This is the one the
  client actually reads.
- `./WoW.mfil` - the **client stub**, just `version=1` and `manifest=<source>/streaming.mfil`.
  The client follows that redirect to the hosted manifest. Put this on the client.

MPQ archives are streamed whole, like any other file. List a `.MPQ` in `CONTENT_DIR` and the
client downloads byte ranges of it on demand, then everything inside becomes available through
normal MPQ mounting. Streaming only fires for files the client cannot already resolve from a
mounted MPQ or from disk, so an installed copy always wins. Loose files override MPQ contents.

## Serve the files

Put `WoW.mfil` in the client root (next to `Wow.exe`) and serve the staged files with HTTP range
support so that `<source>/<relpath>` resolves.

Local test server (single-range 206 support built in):

```
python serve_streaming.py ./serve --port 80 --prefix /streaming
```

In production any range-capable static host works (nginx serves ranges by default, also Caddy,
S3, a CDN). Point it so that `example.com/streaming/<relpath>` returns the file.
