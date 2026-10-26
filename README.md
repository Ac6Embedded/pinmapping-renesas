# Renesas pin mapping

Pin-mapping data of Renesas MCUs, taken from Renesas's FSP releases and
repackaged small.

Renesas ships the pin data of its configurators (the Pins tab of e2 studio
and the Smart Configurators) inside the FSP packs archive of each
[FSP release](https://github.com/renesas/fsp/releases), about 180 MB. Pin
tools only need three small files from each MCU series pack:

| File | Content |
|---|---|
| `.mcu/.pinmapping/plugin<series>.xml` | device and package to pin file |
| `.mcu/.pinmapping/<pin file>.xml` | the pins of one package and the functions each pin offers |
| `.mcu/.pinconfig/<PART>.pincfg` | part number to device and package |

Each release here holds the series packs of one FSP version with only those
files, under the original pack names and inner paths, so a tool that reads the
original packs reads these unchanged. About 14 MB instead of 180 MB.

Used by [Devicetree Manager for Zephyr](https://github.com/Ac6Embedded/vscode-devicetree-manager-for-zephyr)
to offer the pin functions of Renesas RA and RA0 chips.

## Releases

One release per FSP version, tagged `ra-fsp-v<version>`, with:

- `renesas-ra-pinmapping-<version>.zip`
  - `packs/Renesas.RA_mcu_<series>.<version>.pack`, one per MCU series
  - `source.json`: the Renesas release and archive it comes from, with the
    archive's SHA-256
- `index-ra.json`: the index entry of that release (below)

## Index

[`index/ra.json`](index/ra.json) on `main` always describes the newest
release. Tools read it from
`https://raw.githubusercontent.com/Ac6Embedded/pinmapping-renesas/main/index/ra.json`,
download `archive.url` and check `archive.sha256`:

```json
{
  "schema": 1,
  "family": "ra",
  "version": "6.6.0",
  "archive": {
    "name": "renesas-ra-pinmapping-6.6.0.zip",
    "url": "https://github.com/Ac6Embedded/pinmapping-renesas/releases/download/ra-fsp-v6.6.0/renesas-ra-pinmapping-6.6.0.zip",
    "sha256": "...",
    "size": 13819604
  }
}
```

The index never moves back to an older version on its own. To point tools at
another release, edit `index/ra.json`.

## Updates

[Update pin mapping](.github/workflows/update.yml) runs every 3 days. It asks
GitHub for the latest `renesas/fsp` release and, when this repository has no
release for that version yet, builds one, publishes it and moves the index to
it.

To publish a given FSP version, or to rebuild one: Actions, Update pin mapping,
Run workflow. Give the FSP version (empty for the latest) and tick Force to
replace a release that already exists.

## Build locally

Python 3.9 or newer, standard library only.

```sh
python scripts/pinmapping.py resolve                       # latest FSP release
python scripts/pinmapping.py build --out dist              # download it and build
python scripts/pinmapping.py build --fsp-zip FSP_Packs_v6.6.0.zip --out dist
python scripts/pinmapping.py index dist/index-ra.json      # refresh index/ra.json
python -m unittest discover -s tests -v
```

The same input always gives the same archive bytes.
