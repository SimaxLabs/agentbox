import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import bundle_native_metadata


class NativeReleaseMetadataTest(unittest.TestCase):
    def test_launchpad_source_files_are_exact_hashed_and_can_be_superseded(self):
        publications = {
            "entries": [
                {
                    "distro_series_link": "https://api.launchpad.net/1.0/ubuntu/jammy",
                    "self_link": "https://api.launchpad.net/1.0/source-publication",
                    "status": "Superseded",
                }
            ]
        }
        source_files = [
            {
                "url": "https://launchpad.net/readline_8.1.2-1.dsc",
                "sha256": "a" * 64,
                "size": 123,
            }
        ]
        responses = [
            io.BytesIO(json.dumps(publications).encode()),
            io.BytesIO(json.dumps(source_files).encode()),
        ]

        with patch.object(
            bundle_native_metadata.platform,
            "freedesktop_os_release",
            return_value={"ID": "ubuntu", "VERSION_CODENAME": "jammy"},
        ), patch.object(
            bundle_native_metadata, "urlopen", side_effect=responses
        ) as mocked_urlopen:
            records = bundle_native_metadata.launchpad_source_files(
                "readline", "8.1.2-1"
            )

        self.assertEqual(records[0]["filename"], "readline_8.1.2-1.dsc")
        self.assertEqual(records[0]["sha256"], "a" * 64)
        first_url = mocked_urlopen.call_args_list[0].args[0]
        self.assertIn("source_name=readline", first_url)
        self.assertIn("version=8.1.2-1", first_url)

    def test_ubuntu_owner_fallback_still_requires_the_exact_file(self):
        path = Path("/usr/lib/x86_64-linux-gnu/libgcc_s.so.1")
        outputs = [
            "unrelated:amd64: /different/libgcc_s.so.1",
            "wrong:i386: /usr/lib/i386-linux-gnu/libgcc_s.so.1\n"
            "libgcc-s1:amd64: /usr/lib/x86_64-linux-gnu/libgcc_s.so.1",
        ]
        with patch.object(
            bundle_native_metadata, "command_output", side_effect=outputs
        ):
            package = bundle_native_metadata.ubuntu_binary_package(path)

        self.assertEqual(package, "libgcc-s1:amd64")

    def test_ubuntu_owner_fallback_rejects_a_basename_only_match(self):
        output = "wrong:i386: /usr/lib/i386-linux-gnu/libgcc_s.so.1"
        with patch.object(
            bundle_native_metadata, "command_output", return_value=output
        ):
            with self.assertRaisesRegex(RuntimeError, "Cannot identify"):
                bundle_native_metadata.ubuntu_binary_package(
                    Path("/usr/lib/x86_64-linux-gnu/libgcc_s.so.1")
                )

    def test_analysis_inventory_includes_owned_extensions(self):
        class Package:
            metadata = {"Name": "ExampleNative"}

        with tempfile.TemporaryDirectory() as temporary_directory:
            extension = Path(temporary_directory) / "example.so"
            extension.touch()
            package_map = {extension.resolve(): [Package()]}
            grouped = bundle_native_metadata.classify_native_entries(
                [
                    ("example/example.so", str(extension), "EXTENSION"),
                    ("ignored.txt", str(extension), "DATA"),
                ],
                package_map,
            )

        entries = grouped["Python package ExampleNative"]
        self.assertEqual(entries[0][0], "example/example.so")
        self.assertEqual(entries[0][2], "EXTENSION")

    def test_windows_dlls_extensions_are_attributed_to_cpython(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            extension = root / "DLLs" / "_ssl.pyd"
            paths = {
                "purelib": str(root / "Lib" / "site-packages"),
                "platlib": str(root / "Lib" / "site-packages"),
                "stdlib": str(root / "Lib"),
                "platstdlib": str(root / "Lib"),
            }
            with patch.object(
                bundle_native_metadata.sysconfig, "get_paths", return_value=paths
            ), patch.object(
                bundle_native_metadata.sysconfig,
                "get_config_var",
                return_value=None,
            ), patch.object(
                bundle_native_metadata.sys, "prefix", str(root)
            ), patch.object(
                bundle_native_metadata.sys, "base_prefix", str(root)
            ):
                component = bundle_native_metadata.extension_component(extension, {})

        self.assertEqual(component, "CPython")

    def test_binary_classification_requires_a_known_library_name(self):
        self.assertEqual(
            bundle_native_metadata.component_for(Path("libcrypto.so.3")), "OpenSSL"
        )
        with self.assertRaisesRegex(RuntimeError, "Unrecognized"):
            bundle_native_metadata.component_for(Path("cryptominer.so"))

    def test_windows_libffi_uses_cpython_exact_dependency_pin(self):
        pins = b"set binaries=%binaries% libffi-3.4.4\r\n"
        commit = "7" * 40
        responses = [
            io.BytesIO(pins),
            io.BytesIO(json.dumps({"sha": commit}).encode()),
        ]
        with patch.object(
            bundle_native_metadata.platform,
            "python_version",
            return_value="3.14.7",
        ), patch.object(bundle_native_metadata, "urlopen", side_effect=responses):
            metadata = bundle_native_metadata.cpython_source_dependency(
                "libffi", "MIT", "LICENSE"
            )

        self.assertEqual(metadata["version"], "3.4.4")
        self.assertEqual(metadata["sources"][0]["commit"], commit)
        self.assertIn("v3.14.7", metadata["cpython_dependency_pin"])

    def test_windows_static_dependencies_get_secondary_component_records(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            extension = root / "DLLs" / "_zstd.pyd"
            paths = {
                "purelib": str(root / "Lib" / "site-packages"),
                "platlib": str(root / "Lib" / "site-packages"),
                "stdlib": str(root / "Lib"),
                "platstdlib": str(root / "Lib"),
            }
            with patch.object(
                bundle_native_metadata.platform, "system", return_value="Windows"
            ), patch.object(
                bundle_native_metadata.sysconfig, "get_paths", return_value=paths
            ), patch.object(
                bundle_native_metadata.sysconfig, "get_config_var", return_value=None
            ), patch.object(
                bundle_native_metadata.sys, "prefix", str(root)
            ), patch.object(
                bundle_native_metadata.sys, "base_prefix", str(root)
            ):
                grouped = bundle_native_metadata.classify_native_entries(
                    [
                        ("python3.14/DLLs/_zstd.pyd", str(extension), "EXTENSION"),
                        ("python314.dll", str(root / "python314.dll"), "BINARY"),
                    ],
                    {},
                )

        self.assertEqual(len(grouped["CPython"]), 2)
        self.assertEqual(len(grouped["CPython dependency Zstandard"]), 1)
        self.assertEqual(len(grouped["CPython dependency zlib-ng"]), 1)

    def test_ubuntu_component_requires_one_exact_source(self):
        records = [
            {
                "binary_package": "libgcc-s1",
                "binary_version": "12.3-1",
                "source_package": "gcc-12",
                "source_version": "12.3-1",
                "copyright_path": "/usr/share/doc/libgcc-s1/copyright",
            },
            {
                "binary_package": "libstdc++6",
                "binary_version": "13.2-1",
                "source_package": "gcc-13",
                "source_version": "13.2-1",
                "copyright_path": "/usr/share/doc/libstdc++6/copyright",
            },
        ]
        with patch.object(
            bundle_native_metadata,
            "ubuntu_package_record",
            side_effect=records,
        ):
            with self.assertRaisesRegex(RuntimeError, "spans Ubuntu sources"):
                bundle_native_metadata.ubuntu_component_metadata(
                    [Path("libgcc_s.so.1"), Path("libstdc++.so.6")]
                )

    def test_linux_native_libraries_use_ubuntu_package_provenance(self):
        expected = {
            "version": "3.4.4-1",
            "license_expression": "MIT",
            "sources": [{"url": "https://launchpad.net/libffi.dsc"}],
        }
        with patch.object(
            bundle_native_metadata.platform, "system", return_value="Linux"
        ), patch.object(
            bundle_native_metadata,
            "ubuntu_native_metadata",
            return_value=expected,
        ) as mocked_metadata:
            metadata = bundle_native_metadata.component_metadata(
                "libffi", [Path("libffi.so.8")], {}
            )

        self.assertIs(metadata, expected)
        mocked_metadata.assert_called_once_with("libffi", [Path("libffi.so.8")])

    def test_local_package_notice_is_bundled(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "copyright"
            source.write_text("Copyright holder\nBSD terms\n", encoding="utf-8")
            destination = root / "package"
            metadata = {
                "version": "1.0-1",
                "license_expression": "BSD-3-Clause",
                "local_license_paths": [str(source)],
            }

            bundle_native_metadata.write_notices(
                "util-linux libuuid", metadata, destination
            )

            notice = destination / metadata["license_files"][0]
            self.assertEqual(notice.read_bytes(), source.read_bytes())
            self.assertNotIn("local_license_paths", metadata)


if __name__ == "__main__":
    unittest.main()
