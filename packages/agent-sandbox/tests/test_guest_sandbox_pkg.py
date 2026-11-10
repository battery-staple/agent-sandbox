"""
Unit tests for guest/bin/sandbox-pkg, the rootless package helper.

These tests never touch the network or the real apt database: package
resolution is exercised through the pure helpers, and the install/remove flow
is driven with the apt-facing functions patched out.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "guest" / "bin" / "sandbox-pkg"

loader = importlib.machinery.SourceFileLoader("sandbox_pkg", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
assert spec is not None and spec.loader is not None
pkg = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = pkg  # dataclasses resolve annotations via sys.modules
loader.exec_module(pkg)


def _paths(tmp: Path) -> "pkg.Paths":
    return pkg.Paths(root=tmp / "opt", bin=tmp / "bin", cache=tmp / "cache")


def _executable(path: Path, payload: str = "#!/bin/sh\nexit 0\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    path.chmod(0o755)
    return path


class TestParseSpecs(unittest.TestCase):
    def test_accepts_plain_and_versioned_names(self):
        self.assertEqual(pkg.parse_specs(["jq"]), ["jq"])
        self.assertEqual(
            pkg.parse_specs(["jq=1.7.1-3ubuntu0.24.04.2"]),
            ["jq=1.7.1-3ubuntu0.24.04.2"],
        )
        self.assertEqual(pkg.parse_specs(["linux-tools-6.8", "g++"]), ["linux-tools-6.8", "g++"])

    def test_rejects_option_injection(self):
        for spec in ["--force", "-o", "jq --force"]:
            with self.subTest(spec=spec), self.assertRaises(pkg.PkgError):
                pkg.parse_specs([spec])

    def test_rejects_paths_and_urls(self):
        for spec in ["./evil.deb", "/tmp/evil.deb", "../jq", "http://evil/jq.deb", "a/b"]:
            with self.subTest(spec=spec), self.assertRaises(pkg.PkgError):
                pkg.parse_specs([spec])

    def test_rejects_shell_metacharacters(self):
        for spec in ["jq;rm -rf /", "jq&&id", "$(id)", "jq|x"]:
            with self.subTest(spec=spec), self.assertRaises(pkg.PkgError):
                pkg.parse_specs([spec])

    def test_rejects_empty_input(self):
        with self.assertRaises(pkg.PkgError):
            pkg.parse_specs([])


class TestDependencyClosure(unittest.TestCase):
    APT_OUTPUT = """\
cowsay
  Depends: libc6
  Depends: perl
  |Depends: <perl5>
perl
  Depends: libperl5.38
libperl5.38
  Depends: libc6
libc6
"""

    def test_keeps_only_missing_dependencies(self):
        installed = {"libc6": True, "perl": True}
        with patch.object(pkg, "is_installed", side_effect=lambda name: installed.get(name, False)):
            deps = pkg.filter_closure(self.APT_OUTPUT.splitlines(), "cowsay")
        self.assertEqual(deps, ["libperl5.38"])

    def test_drops_root_virtuals_and_indented_relations(self):
        with patch.object(pkg, "is_installed", return_value=False):
            deps = pkg.filter_closure(self.APT_OUTPUT.splitlines(), "cowsay")
        self.assertNotIn("cowsay", deps)
        self.assertNotIn("<perl5>", deps)
        self.assertIn("perl", deps)

    def test_strips_architecture_suffixes(self):
        with patch.object(pkg, "is_installed", return_value=False):
            deps = pkg.filter_closure(["jq", "libonig5:arm64"], "jq")
        self.assertEqual(deps, ["libonig5"])


class TestDiscovery(unittest.TestCase):
    def test_find_binaries_returns_executables_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp)
            _executable(prefix / "usr" / "bin" / "tool")
            _executable(prefix / "usr" / "sbin" / "tool-admin")
            (prefix / "usr" / "bin" / "README").write_text("docs", encoding="utf-8")
            names = [path.name for path in pkg.find_binaries(prefix)]
        self.assertEqual(names, ["tool", "tool-admin"])

    def test_find_lib_dirs_includes_multiarch_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp)
            multiarch = prefix / "usr" / "lib" / "aarch64-linux-gnu"
            multiarch.mkdir(parents=True)
            (multiarch / "libonig.so.5").write_text("", encoding="utf-8")
            dirs = pkg.find_lib_dirs(prefix)
        self.assertIn(multiarch, dirs)

    def test_find_lib_dirs_empty_without_libraries(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = Path(tmp)
            (prefix / "usr" / "bin").mkdir(parents=True)
            self.assertEqual(pkg.find_lib_dirs(prefix), [])


class TestLaunchers(unittest.TestCase):
    def test_plain_package_gets_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            prefix = paths.root / "jq" / "1.7"
            binary = _executable(prefix / "usr" / "bin" / "jq")
            name, kind = pkg.install_launcher(paths, prefix, binary)
            self.assertEqual((name, kind), ("jq", "symlink"))
            self.assertTrue((paths.bin / "jq").is_symlink())
            self.assertEqual((paths.bin / "jq").resolve(), binary)

    def test_package_with_libraries_gets_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            prefix = paths.root / "figlet" / "2.2.5"
            binary = _executable(prefix / "usr" / "bin" / "figlet")
            libdir = prefix / "usr" / "lib" / "aarch64-linux-gnu"
            libdir.mkdir(parents=True)
            (libdir / "libfiglet.so.1").write_text("", encoding="utf-8")
            (prefix / "usr" / "share" / "figlet").mkdir(parents=True)

            _, kind = pkg.install_launcher(paths, prefix, binary)
            launcher = paths.bin / "figlet"
            text = launcher.read_text(encoding="utf-8")

        self.assertEqual(kind, "wrapper")
        self.assertIn(pkg.LAUNCHER_MARKER, text)
        self.assertIn(f"LD_LIBRARY_PATH='{libdir}", text)
        self.assertIn(f"XDG_DATA_DIRS='{prefix / 'usr' / 'share'}", text)
        self.assertIn(f"exec '{binary}' \"$@\"", text)

    def test_never_clobbers_foreign_launcher(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            prefix = paths.root / "jq" / "1.7"
            binary = _executable(prefix / "usr" / "bin" / "jq")
            paths.bin.mkdir(parents=True)
            foreign = paths.bin / "jq"
            foreign.write_text("#!/bin/sh\necho system jq\n", encoding="utf-8")

            name, kind = pkg.install_launcher(paths, prefix, binary)
            content = foreign.read_text(encoding="utf-8")

        self.assertEqual((name, kind), ("jq", "skipped"))
        self.assertIn("system jq", content)

    def test_is_our_artifact_recognises_wrapper_and_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            prefix = paths.root / "jq" / "1.7"
            binary = _executable(prefix / "usr" / "bin" / "jq")
            pkg.install_launcher(paths, prefix, binary)
            self.assertTrue(pkg.is_our_artifact(paths.bin / "jq", prefix))

            wrapper = paths.bin / "wrapper"
            wrapper.write_text(
                f"#!/bin/sh\n{pkg.LAUNCHER_MARKER}\nexec '{prefix}/usr/bin/jq' \"$@\"\n",
                encoding="utf-8",
            )
            self.assertTrue(pkg.is_our_artifact(wrapper, prefix))

            other_wrapper = paths.bin / "other"
            other_wrapper.write_text(
                f"#!/bin/sh\n{pkg.LAUNCHER_MARKER}\nexec '/elsewhere/jq' \"$@\"\n",
                encoding="utf-8",
            )
            self.assertFalse(pkg.is_our_artifact(other_wrapper, prefix))

            foreign = paths.bin / "foreign"
            foreign.write_text("#!/bin/sh\nexec true\n", encoding="utf-8")
            self.assertFalse(pkg.is_our_artifact(foreign, prefix))


class TestManifest(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            data = {"version": pkg.MANIFEST_VERSION, "packages": {"jq": {"version": "1.7"}}}
            pkg.save_manifest(paths, data)
            self.assertEqual(pkg.load_manifest(paths), data)

    def test_missing_or_corrupt_manifest_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            self.assertEqual(pkg.load_manifest(paths), {"version": pkg.MANIFEST_VERSION, "packages": {}})
            paths.root.mkdir(parents=True)
            paths.manifest.write_text("{not json", encoding="utf-8")
            self.assertEqual(pkg.load_manifest(paths)["packages"], {})


class TestGetAndRemove(unittest.TestCase):
    def test_get_extracts_bins_and_records_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            deb = Path(tmp) / "jq.deb"
            deb.write_text("", encoding="utf-8")
            fake_prefix_bin = paths.root / "jq" / "1.7" / "usr" / "bin" / "jq"

            def fake_extract(deb_path: Path, destination: Path, *, verbose: bool = False) -> None:
                _executable(destination / "usr" / "bin" / "jq")

            with patch.object(pkg, "ensure_index"), \
                 patch.object(pkg, "is_installed", return_value=False), \
                 patch.object(pkg, "dependency_closure", return_value=["libonig5"]), \
                 patch.object(pkg, "download_deb", return_value=deb) as download, \
                 patch.object(pkg, "deb_field", side_effect=lambda deb, field: {"Version": "1.7", "Architecture": "arm64"}[field]), \
                 patch.object(pkg, "extract_deb", side_effect=fake_extract):
                code = pkg.cmd_get(paths, ["jq"], with_deps=True, force=False, verbose=False)

            self.assertEqual(code, 0)
            self.assertEqual(download.call_count, 2)  # package + bundled dependency
            self.assertTrue(fake_prefix_bin.is_file())
            entry = pkg.load_manifest(paths)["packages"]["jq"]
        self.assertEqual(entry["version"], "1.7")
        self.assertEqual(entry["bundled"], ["libonig5"])
        self.assertEqual(entry["binaries"], {"jq": str(fake_prefix_bin)})

    def test_get_skips_packages_already_in_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            with patch.object(pkg, "ensure_index"), \
                 patch.object(pkg, "is_installed", return_value=True), \
                 patch.object(pkg, "download_deb") as download:
                code = pkg.cmd_get(paths, ["jq"], with_deps=True, force=False, verbose=False)
        self.assertEqual(code, 0)
        download.assert_not_called()

    def test_get_rejects_oversized_bundles(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            huge = [f"pkg{i}" for i in range(pkg.MAX_BUNDLE_PACKAGES + 1)]
            with patch.object(pkg, "ensure_index"), \
                 patch.object(pkg, "is_installed", return_value=False), \
                 patch.object(pkg, "dependency_closure", return_value=huge), \
                 patch.object(pkg, "download_deb") as download:
                code = pkg.cmd_get(paths, ["jq"], with_deps=True, force=False, verbose=False)
        self.assertEqual(code, 1)
        download.assert_not_called()

    def test_remove_deletes_launchers_prefix_and_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            prefix = paths.root / "jq" / "1.7"
            binary = _executable(prefix / "usr" / "bin" / "jq")
            pkg.install_launcher(paths, prefix, binary)
            pkg.save_manifest(paths, {
                "version": pkg.MANIFEST_VERSION,
                "packages": {"jq": {"version": "1.7", "prefix": str(prefix), "binaries": {"jq": str(binary)}}},
            })

            self.assertEqual(pkg.cmd_remove(paths, ["jq"]), 0)
            self.assertFalse((paths.bin / "jq").exists())
            self.assertFalse((paths.bin / "jq").is_symlink())
            self.assertFalse(prefix.exists())
            self.assertEqual(pkg.load_manifest(paths)["packages"], {})

    def test_remove_unknown_package_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = _paths(Path(tmp))
            self.assertEqual(pkg.cmd_remove(paths, ["nope"]), 1)


class TestInterpreterCheck(unittest.TestCase):
    def test_reports_missing_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = _executable(Path(tmp) / "tool", "#!/usr/bin/does-not-exist\nprint 1;\n")
            self.assertEqual(pkg.missing_interpreter(script), "/usr/bin/does-not-exist")

    def test_ignores_env_shebangs_and_elf(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = _executable(Path(tmp) / "tool", "#!/usr/bin/env sh\nexit 0\n")
            self.assertIsNone(pkg.missing_interpreter(script))
            elf = Path(tmp) / "elf"
            elf.write_bytes(b"\x7fELFrest")
            self.assertIsNone(pkg.missing_interpreter(elf))


if __name__ == "__main__":
    unittest.main()
