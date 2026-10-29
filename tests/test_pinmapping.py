"""Tests for scripts/pinmapping.py on a synthetic FSP archive (no network)."""

import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import pinmapping  # noqa: E402


def zip_bytes(entries: dict) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return out.getvalue()


def fake_pack(series: str) -> bytes:
    return zip_bytes({
        f".mcu/.pinmapping/plugin{series}.xml": "<plugin/>",
        f".mcu/.pinmapping/PinCfgR7FA{series.upper()}xxxFC.xml": "<pinMappings/>" * 50,
        f".mcu/.pinconfig/R7FA{series.upper()}BH3CFC.pincfg": "<pinSettings/>",
        f".mcu/.iofiles/R7FA{series.upper()}.svd": "svd" * 1000,
        f"ra/fsp/src/bsp/mcu/{series}/bsp_mcu_info.h": "#define X 1",
        ".module_descriptions/Renesas##BSP.xml": "<module/>",
        f".module_descriptions/Renesas##BSP##{series}##fsp####6.6.0.xml": "<raModuleDescription><clock/></raModuleDescription>",
        f".module_descriptions/Renesas##BSP##{series}##device####6.6.0.xml": "<raModuleDescription/>",
        f".module_descriptions/Renesas##BSP##{series}##device##R7FA{series.upper()}BH3CFC##6.6.0.xml": "<raModuleDescription/>",
        f".module_descriptions/Renesas##BSP##{series}##linker####6.6.0.xml": "<raModuleDescription/>",
        "Renesas.RA_mcu.pdsc": "<package/>",
    })


def fake_fsp(path: Path) -> None:
    path.write_bytes(zip_bytes({
        "internal/projectgen/ra/packs/Renesas.RA_mcu_ra6m5.6.6.0.pack": fake_pack("ra6m5"),
        "internal/projectgen/ra/packs/Renesas.RA_mcu_ra4e1.6.6.0.pack": fake_pack("ra4e1"),
        "internal/projectgen/ra/packs/Renesas.RA.6.6.0.pack": zip_bytes({"big": "x" * 5000}),
        "internal/projectgen/ra/packs/Arm.CMSIS6.6.1.0+fsp.6.6.0.pack": zip_bytes({"a": "b"}),
        "internal/devassist/dev_assist_v6.6.0.rds": "rds",
    }))


SOURCE = {"repository": "renesas/fsp", "release": "v6.6.0", "asset": "FSP_Packs_v6.6.0.zip", "sha256": "0" * 64}


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.fsp = self.dir / "FSP_Packs_v6.6.0.zip"
        fake_fsp(self.fsp)

    def tearDown(self):
        self.tmp.cleanup()

    def test_keeps_only_series_packs_and_their_pin_and_clock_entries(self):
        index = pinmapping.build("ra", "6.6.0", self.fsp, self.dir / "dist", SOURCE)
        archive = zipfile.ZipFile(self.dir / "dist" / "renesas-ra-pinmapping-6.6.0.zip")
        self.assertEqual(archive.namelist(), [
            "packs/Renesas.RA_mcu_ra4e1.6.6.0.pack",
            "packs/Renesas.RA_mcu_ra6m5.6.6.0.pack",
            "source.json",
        ])
        pack = zipfile.ZipFile(io.BytesIO(archive.read("packs/Renesas.RA_mcu_ra6m5.6.6.0.pack")))
        self.assertEqual(pack.namelist(), [
            ".mcu/.pinconfig/R7FARA6M5BH3CFC.pincfg",
            ".mcu/.pinmapping/PinCfgR7FARA6M5xxxFC.xml",
            ".mcu/.pinmapping/pluginra6m5.xml",
            ".module_descriptions/Renesas##BSP##ra6m5##fsp####6.6.0.xml",
        ])
        self.assertEqual(pack.read(".mcu/.pinmapping/PinCfgR7FARA6M5xxxFC.xml"), b"<pinMappings/>" * 50)
        self.assertEqual(index["packs"], 2)
        self.assertEqual(index["version"], "6.6.0")
        self.assertTrue(index["archive"]["url"].endswith("/releases/download/ra-fsp-v6.6.0/renesas-ra-pinmapping-6.6.0.zip"))
        self.assertEqual(index["archive"]["sha256"],
                         hashlib.sha256((self.dir / "dist" / "renesas-ra-pinmapping-6.6.0.zip").read_bytes()).hexdigest())
        self.assertTrue((self.dir / "dist" / "NOTES-ra.md").exists())

    def test_same_input_gives_the_same_archive_bytes(self):
        a = pinmapping.build("ra", "6.6.0", self.fsp, self.dir / "a", SOURCE)
        b = pinmapping.build("ra", "6.6.0", self.fsp, self.dir / "b", SOURCE)
        self.assertEqual(a["archive"]["sha256"], b["archive"]["sha256"])

    def test_an_archive_without_series_packs_is_refused(self):
        empty = self.dir / "FSP_Packs_v1.0.0.zip"
        empty.write_bytes(zip_bytes({"internal/other.txt": "x"}))
        with self.assertRaises(SystemExit):
            pinmapping.build("ra", "1.0.0", empty, self.dir / "dist", SOURCE)

    def test_index_only_moves_forward(self):
        index_dir = self.dir / "index"

        def entry(version, sha="a"):
            return {"family": "ra", "version": version, "archive": {"sha256": sha * 64}}

        self.assertTrue(pinmapping.refresh_index(entry("6.6.0"), index_dir))
        self.assertFalse(pinmapping.refresh_index(entry("6.5.1", "b"), index_dir))
        self.assertFalse(pinmapping.refresh_index(entry("6.6.0"), index_dir))
        self.assertTrue(pinmapping.refresh_index(entry("6.6.0", "c"), index_dir), "a rebuild replaces it")
        self.assertTrue(pinmapping.refresh_index(entry("6.10.0"), index_dir))
        self.assertEqual(json.loads((index_dir / "ra.json").read_text())["version"], "6.10.0")

    def test_index_command_reports_the_current_version(self):
        release_index = self.dir / "index-ra.json"
        index_dir = self.dir / "index"
        out = io.StringIO()
        for version, expected in (("6.6.0", "changed=true\ncurrent=6.6.0\n"), ("6.5.1", "changed=false\ncurrent=6.6.0\n")):
            release_index.write_text(json.dumps({"family": "ra", "version": version, "archive": {"sha256": "a" * 64}}))
            out.seek(0)
            out.truncate()
            with redirect_stdout(out):
                self.assertEqual(pinmapping.main(["index", str(release_index), "--index-dir", str(index_dir)]), 0)
            self.assertEqual(out.getvalue(), expected)

    def test_odd_versions_are_refused(self):
        for bad in ("6.6.0; rm -rf /", "../6.6.0", "v6.6.0", ""):
            self.assertIsNone(pinmapping.VERSION.match(bad), bad)
        for good in ("6.6.0", "6.10.0", "5.9.0-rc.1"):
            self.assertIsNotNone(pinmapping.VERSION.match(good), good)

    def test_versions_order_numerically(self):
        self.assertGreater(pinmapping.version_key("6.10.0"), pinmapping.version_key("6.9.1"))
        self.assertGreater(pinmapping.version_key("7.0.0"), pinmapping.version_key("6.99.99"))
        self.assertGreater(pinmapping.version_key("6.6.0"), pinmapping.version_key("6.6.0-rc.1"))
        self.assertEqual(pinmapping.version_key("6.6"), pinmapping.version_key("6.6.0"))


if __name__ == "__main__":
    unittest.main()
