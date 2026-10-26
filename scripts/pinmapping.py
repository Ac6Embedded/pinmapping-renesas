#!/usr/bin/env python3
"""Build slim Renesas pin-mapping archives from Renesas FSP pack releases.

Renesas publishes its configurator packs (the data behind the Pins tab of
e2 studio and the Smart Configurators) as one big archive per FSP release.
Pin tools only need three small files from each MCU series pack:

  .mcu/.pinmapping/plugin<series>.xml   device + package -> pin file
  .mcu/.pinmapping/<pin file>.xml       pins of a package and their functions
  .mcu/.pinconfig/<PART>.pincfg         part number -> device + package

This script keeps exactly those, in packs with the original names and inner
paths, so a reader of the original packs reads these unchanged.

Commands:
  resolve  find a Renesas release (latest by default) and print what to build,
           as key=value lines (GitHub Actions output format)
  build    download that release, build the slim archive, its index and
           release notes
  index    move index/<family>.json to a release's index when that release is
           newer, or a rebuild of the same version

Standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

# Families this repository publishes. Adding one (e.g. the RZ families from
# renesas/rzt-fsp, rzn-fsp, rzg-fsp, ...) is an entry here plus a matrix entry
# in .github/workflows/update.yml.
FAMILIES = {
    "ra": {
        "title": "Renesas RA",
        "repo": "renesas/fsp",
        "asset": "FSP_Packs_v{version}.zip",
        "packs": re.compile(r"^internal/projectgen/ra/packs/(Renesas\.RA_mcu_[a-z0-9]+)\.[0-9][0-9A-Za-z.]*\.pack$"),
    },
}

# Entries kept from each pack.
KEEP = re.compile(r"^\.mcu/\.(?:pinmapping/[^/]+\.xml|pinconfig/[^/]+\.pincfg)$")

# Release versions accepted from Renesas or from the command line.
VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+)*(?:-[0-9A-Za-z.]+)?$")

# Fixed entry time so the same input always gives the same bytes.
FIXED_TIME = (1980, 1, 1, 0, 0, 0)

USER_AGENT = "pinmapping-renesas"


def this_repo() -> str:
    return os.environ.get("GITHUB_REPOSITORY", "Ac6Embedded/pinmapping-renesas")


def tag_for(family: str, version: str) -> str:
    return f"{family}-fsp-v{version}"


def archive_name(family: str, version: str) -> str:
    return f"renesas-{family}-pinmapping-{version}.zip"


def version_key(version: str) -> tuple:
    """Order versions numerically: 6.10.0 > 6.9.1, 6.6.0 > 6.6.0-rc.1
    (missing parts count as 0, text parts as -1)."""
    parts = [int(p) if p.isdigit() else -1 for p in re.split(r"[.\-+]", version)]
    return tuple(parts + [0] * (8 - len(parts)))


def _request(url: str) -> urllib.request.Request:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


def github_api(path: str) -> dict:
    with urllib.request.urlopen(_request(f"https://api.github.com/{path}"), timeout=60) as r:
        return json.load(r)


def resolve(family: str, version: str | None) -> dict:
    """The Renesas release to mirror: the latest one, or `version`."""
    spec = FAMILIES[family]
    if version and not VERSION.match(version):
        raise SystemExit(f"not a version: {version!r}")
    latest = github_api(f"repos/{spec['repo']}/releases/latest")
    release = latest if not version else github_api(f"repos/{spec['repo']}/releases/tags/v{version}")
    found = release["tag_name"].lstrip("v")
    if not VERSION.match(found):
        raise SystemExit(f"{spec['repo']} release {release['tag_name']!r} is not a version tag")
    name = spec["asset"].format(version=found)
    asset = next((a for a in release.get("assets", []) if a["name"] == name), None)
    if asset is None:
        raise SystemExit(f"{spec['repo']} {release['tag_name']} has no {name}")
    digest = asset.get("digest") or ""
    return {
        "family": family,
        "title": spec["title"],
        "version": found,
        "tag": tag_for(family, found),
        "latest": "true" if release["tag_name"] == latest["tag_name"] else "false",
        "asset_url": asset["browser_download_url"],
        "asset_sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else "",
        "asset_size": str(asset.get("size", 0)),
        "release_url": release["html_url"],
    }


def download(url: str, dest: Path, expected_sha256: str = "") -> str:
    """Stream `url` to `dest` and return its SHA-256 (checked when expected)."""
    sha = hashlib.sha256()
    with urllib.request.urlopen(_request(url), timeout=120) as r, open(dest, "wb") as f:
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            sha.update(chunk)
            f.write(chunk)
    digest = sha.hexdigest()
    if expected_sha256 and digest != expected_sha256:
        raise SystemExit(f"checksum mismatch for {url}: {digest} != {expected_sha256}")
    return digest


def _write(zf: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, FIXED_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    zf.writestr(info, data, compresslevel=9)


def slim_pack(pack: bytes) -> tuple[bytes, int]:
    """A pack holding only the pin-mapping entries, same paths. Returns
    (bytes, number of entries kept)."""
    src = zipfile.ZipFile(io.BytesIO(pack))
    names = sorted(n for n in src.namelist() if KEEP.match(n))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for name in names:
            _write(dst, name, src.read(name))
    return out.getvalue(), len(names)


def build(family: str, version: str, fsp_zip: Path, out_dir: Path, source: dict) -> dict:
    """Write the slim archive, its index and release notes to `out_dir`."""
    spec = FAMILIES[family]
    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / archive_name(family, version)
    packs: list[dict] = []
    with zipfile.ZipFile(fsp_zip) as fsp, zipfile.ZipFile(archive, "w") as dst:
        for name in sorted(fsp.namelist()):
            m = spec["packs"].match(name)
            if not m:
                continue
            data, kept = slim_pack(fsp.read(name))
            if kept == 0:
                continue
            base = name.rsplit("/", 1)[-1]
            _write(dst, f"packs/{base}", data)
            packs.append({"name": base, "entries": kept, "size": len(data)})
        if not packs:
            raise SystemExit(f"no {spec['title']} packs found in {fsp_zip.name}")
        _write(dst, "source.json", json.dumps(source, indent=2, sort_keys=True).encode())

    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    tag = tag_for(family, version)
    index = {
        "schema": 1,
        "family": family,
        "title": spec["title"],
        "version": version,
        "archive": {
            "name": archive.name,
            "url": f"https://github.com/{this_repo()}/releases/download/{tag}/{archive.name}",
            "sha256": sha,
            "size": archive.stat().st_size,
        },
        "layout": "packs/<pack name>.pack, each keeping .mcu/.pinmapping/*.xml and .mcu/.pinconfig/*.pincfg",
        "packs": len(packs),
        "source": source,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    (out_dir / f"index-{family}.json").write_text(json.dumps(index, indent=2) + "\n")
    notes = [
        f"{spec['title']} pin mapping from FSP {version}.",
        "",
        f"Source: {source.get('release_url', '')}",
        f"Source archive: `{source.get('asset', '')}` (SHA-256 `{source.get('sha256', '')}`)",
        "",
        f"`{archive.name}`: {len(packs)} series packs, {archive.stat().st_size / 1e6:.1f} MB, SHA-256 `{sha}`.",
        "",
        "| Pack | Files | Size |",
        "|---|---|---|",
        *[f"| {p['name']} | {p['entries']} | {p['size'] / 1e3:.0f} kB |" for p in packs],
        "",
    ]
    (out_dir / f"NOTES-{family}.md").write_text("\n".join(notes))
    return index


def refresh_index(candidate: dict, index_dir: Path) -> bool:
    """Write index/<family>.json when `candidate` is a newer version, a
    rebuild of the same version (other archive bytes), or the file is missing.
    Never moves the pointer back to an older version."""
    path = index_dir / f"{candidate['family']}.json"
    if path.exists():
        current = json.loads(path.read_text())
        have, new = version_key(current.get("version", "0")), version_key(candidate["version"])
        if have > new or (have == new and current.get("archive", {}).get("sha256") == candidate["archive"]["sha256"]):
            return False
    index_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(candidate, indent=2) + "\n")
    return True


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_resolve = sub.add_parser("resolve", help="find the Renesas release to mirror")
    p_resolve.add_argument("--family", default="ra", choices=sorted(FAMILIES))
    p_resolve.add_argument("--version", default="", help="FSP version (default: latest)")

    p_build = sub.add_parser("build", help="build the slim archive for one release")
    p_build.add_argument("--family", default="ra", choices=sorted(FAMILIES))
    p_build.add_argument("--version", default="", help="FSP version (default: latest)")
    p_build.add_argument("--fsp-zip", type=Path, help="use this FSP archive instead of downloading it")
    p_build.add_argument("--out", type=Path, default=Path("dist"))

    p_index = sub.add_parser("index", help="refresh index/<family>.json from a release's index")
    p_index.add_argument("release_index", type=Path, help="index-<family>.json of a release")
    p_index.add_argument("--index-dir", type=Path, default=Path("index"))

    args = parser.parse_args(argv)
    if args.command == "resolve":
        for key, value in resolve(args.family, args.version or None).items():
            print(f"{key}={value}")
        return 0

    if args.command == "index":
        candidate = json.loads(args.release_index.read_text())
        changed = refresh_index(candidate, args.index_dir)
        current = json.loads((args.index_dir / f"{candidate['family']}.json").read_text())
        print(f"changed={'true' if changed else 'false'}")
        print(f"current={current['version']}")
        return 0

    if args.fsp_zip:
        version = args.version or re.search(r"v([0-9][0-9A-Za-z.]*)\.zip$", args.fsp_zip.name).group(1)
        digest = hashlib.sha256(args.fsp_zip.read_bytes()).hexdigest()
        source = {"repository": FAMILIES[args.family]["repo"], "release": f"v{version}",
                  "release_url": f"https://github.com/{FAMILIES[args.family]['repo']}/releases/tag/v{version}",
                  "asset": args.fsp_zip.name, "sha256": digest}
        index = build(args.family, version, args.fsp_zip, args.out, source)
    else:
        info = resolve(args.family, args.version or None)
        version = info["version"]
        with tempfile.TemporaryDirectory() as tmp:
            fsp = Path(tmp) / FAMILIES[args.family]["asset"].format(version=version)
            print(f"downloading {info['asset_url']}", file=sys.stderr)
            digest = download(info["asset_url"], fsp, info["asset_sha256"])
            source = {"repository": FAMILIES[args.family]["repo"], "release": f"v{version}",
                      "release_url": info["release_url"], "asset": fsp.name, "sha256": digest}
            index = build(args.family, version, fsp, args.out, source)

    print(f"{index['archive']['name']}: {index['packs']} packs, {index['archive']['size']} bytes, "
          f"sha256 {index['archive']['sha256']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
