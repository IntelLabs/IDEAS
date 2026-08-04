#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from pathlib import Path

from ideas.bear import BuildDatabase, CompileCommand, LinkCommand


def test_resolve_targets_sphincs_build():
    """
    Models the sphincs-plus build where utils.c is compiled twice:
    - For sphincs_obj (app/): with -DBLAKE_TR=1
    - For blake (lib/blake/): without -DBLAKE_TR=1
    The driver links against both, so resolve_targets must dedup utils.c and
    pick the sphincs version (first encountered in link order).
    """
    build = Path("/build/test_case")
    src_app = Path("/src/test_case/app/src")
    src_blake = Path("/src/test_case/lib/blake/src")

    utils_c = src_app / "utils.c"
    sign_c = src_app / "sign.c"
    rng_c = src_app / "rng.c"
    pqcgen_c = src_app / "PQCgenKAT_sign.c"
    hash_blake_c = src_blake / "hash_blake.c"

    sphincs_dir = build / "app/CMakeFiles/sphincs_obj.dir/src"
    blake_dir = build / "lib/blake/CMakeFiles/blake.dir"
    driver_dir = build / "app/CMakeFiles/driver.dir/src"

    sphincs_flags = ["-DPARAMS=sphincs-blake-128f", "-DBLAKE_TR=1", "-w", "-O3", "-std=gnu99"]
    blake_flags = ["-DPARAMS=sphincs-blake-128f", "-w", "-O3", "-std=gnu99"]

    libblake = build / "lib/blake/libblake.so"
    libsphincs = build / "app/libsphincs_core_det.so"
    driver = build / "app/driver"

    compile_commands = [
        # sphincs_obj objects — compiled with -DBLAKE_TR=1
        CompileCommand.from_arguments(
            ["clang", *sphincs_flags, "-c", str(sign_c), "-o", str(sphincs_dir / "sign.c.o")],
            working_dir=build,
        ),
        CompileCommand.from_arguments(
            ["clang", *sphincs_flags, "-c", str(utils_c), "-o", str(sphincs_dir / "utils.c.o")],
            working_dir=build,
        ),
        CompileCommand.from_arguments(
            ["clang", *sphincs_flags, "-c", str(rng_c), "-o", str(sphincs_dir / "rng.c.o")],
            working_dir=build,
        ),
        # blake objects — compiled WITHOUT -DBLAKE_TR=1
        CompileCommand.from_arguments(
            [
                "clang",
                *blake_flags,
                "-c",
                str(hash_blake_c),
                "-o",
                str(blake_dir / "src/hash_blake.c.o"),
            ],
            working_dir=build,
        ),
        CompileCommand.from_arguments(
            [
                "clang",
                *blake_flags,
                "-c",
                str(utils_c),
                "-o",
                str(blake_dir / "__/__/app/src/utils.c.o"),
            ],
            working_dir=build,
        ),
        # driver object
        CompileCommand.from_arguments(
            [
                "clang",
                *blake_flags,
                "-c",
                str(pqcgen_c),
                "-o",
                str(driver_dir / "PQCgenKAT_sign.c.o"),
            ],
            working_dir=build,
        ),
    ]

    link_commands = [
        LinkCommand.from_arguments(
            [
                "clang",
                "-shared",
                "-o",
                str(libblake),
                str(blake_dir / "src/hash_blake.c.o"),
                str(blake_dir / "__/__/app/src/utils.c.o"),
            ],
            working_dir=build,
        ),
        LinkCommand.from_arguments(
            [
                "clang",
                "-shared",
                "-o",
                str(libsphincs),
                str(sphincs_dir / "sign.c.o"),
                str(sphincs_dir / "utils.c.o"),
                str(sphincs_dir / "rng.c.o"),
            ],
            working_dir=build,
        ),
        LinkCommand.from_arguments(
            [
                "clang",
                "-o",
                str(driver),
                str(driver_dir / "PQCgenKAT_sign.c.o"),
                str(libsphincs),
                str(libblake),
                "-lcrypto",
            ],
            working_dir=build,
        ),
    ]

    db = BuildDatabase(
        compile_commands=[c for c in compile_commands if c is not None],
        link_commands=[lc for lc in link_commands if lc is not None],
    )
    targets = db.resolve_targets()

    assert set(targets) == {"libblake.so", "libsphincs_core_det.so", "driver"}

    # libblake.so: blake sources only, no -DBLAKE_TR=1 on utils.c
    blake_sources = [e.source for e in targets["libblake.so"].entries]
    assert blake_sources == [hash_blake_c, utils_c]
    assert "-DBLAKE_TR=1" not in next(
        e.arguments for e in targets["libblake.so"].entries if e.source == utils_c
    )
    assert targets["libblake.so"].link_libs == []

    # libsphincs_core_det.so: sphincs sources, utils.c has -DBLAKE_TR=1
    sphincs_sources = [e.source for e in targets["libsphincs_core_det.so"].entries]
    assert sphincs_sources == [sign_c, utils_c, rng_c]
    assert "-DBLAKE_TR=1" in next(
        e.arguments for e in targets["libsphincs_core_det.so"].entries if e.source == utils_c
    )
    assert targets["libsphincs_core_det.so"].link_libs == []

    # driver: transitive sources in order; utils.c appears exactly once (sphincs version)
    driver_sources = [e.source for e in targets["driver"].entries]
    assert driver_sources == [pqcgen_c, sign_c, utils_c, rng_c, hash_blake_c]
    assert driver_sources.count(utils_c) == 1
    assert "-DBLAKE_TR=1" in next(
        e.arguments for e in targets["driver"].entries if e.source == utils_c
    )
    assert targets["driver"].link_libs == ["crypto"]


def test_compile_command_strips_dep_tracking_flags():
    # CMake injects -MD, -MF, -MT (and -MMD, -MP) into every compile command.
    # These must be stripped so libclang doesn't try to write .d files at
    # relative paths that don't exist during analysis.
    build = Path("/build")
    src = Path("/src/foo.c")
    obj = build / "foo.c.o"
    dep = build / "foo.c.o.d"

    cmd = CompileCommand.from_arguments(
        [
            "clang",
            "-I/usr/include",
            "-DFOO=1",
            "-MD",
            "-MMD",
            "-MP",
            "-MG",
            "-MF",
            str(dep),
            "-MT",
            str(obj),
            "-MQ",
            str(obj),
            "-c",
            str(src),
            "-o",
            str(obj),
        ],
        working_dir=build,
    )

    assert cmd is not None
    assert cmd.source == src
    assert cmd.output == obj
    # None of the dep-tracking flags or their arguments should survive
    assert "-MD" not in cmd.arguments
    assert "-MMD" not in cmd.arguments
    assert "-MP" not in cmd.arguments
    assert "-MG" not in cmd.arguments
    assert "-MF" not in cmd.arguments
    assert "-MT" not in cmd.arguments
    assert "-MQ" not in cmd.arguments
    assert str(dep) not in cmd.arguments
    # Unrelated flags are preserved
    assert "-I/usr/include" in cmd.arguments
    assert "-DFOO=1" in cmd.arguments


def test_compile_command_versioned_compiler_recognized():
    build = Path("/build")
    src = Path("/src/foo.c")
    for compiler in ("clang-21", "gcc-13", "clang-3.8"):
        cmd = CompileCommand.from_arguments(
            [compiler, "-c", str(src), "-o", str(build / "foo.c.o")],
            working_dir=build,
        )
        assert cmd is not None, f"{compiler} should be recognized"


def test_compile_command_non_compiler_returns_none():
    build = Path("/build")
    for executable in ("cmake", "make", "sh", "python3", "ar"):
        result = CompileCommand.from_arguments(
            [executable, "-c", "/src/foo.c", "-o", "/build/foo.c.o"],
            working_dir=build,
        )
        assert result is None, f"{executable} should not be recognized as a compiler"


def test_compile_command_no_output_flag_returns_none():
    build = Path("/build")
    result = CompileCommand.from_arguments(
        ["clang", "-c", "/src/foo.c"],  # no -o
        working_dir=build,
    )
    assert result is None


def test_compile_command_unrecognized_source_extension_returns_none():
    build = Path("/build")
    result = CompileCommand.from_arguments(
        ["clang", "-c", "/src/foo.txt", "-o", str(build / "foo.txt.o")],
        working_dir=build,
    )
    assert result is None


def test_link_command_so_version_stripped():
    build = Path("/build")
    for versioned, expected in [
        ("libfoo.so.1.2.3", "libfoo.so"),
        ("libbar.so.0", "libbar.so"),
        ("libbaz.so", "libbaz.so"),  # no version suffix — unchanged
    ]:
        cmd = LinkCommand.from_arguments(
            ["clang", "-shared", "-o", str(build / versioned), str(build / "foo.c.o")],
            working_dir=build,
        )
        assert cmd is not None
        assert cmd.target == expected, f"{versioned} → expected {expected}, got {cmd.target}"


def test_link_command_compile_only_returns_none():
    build = Path("/build")
    result = LinkCommand.from_arguments(
        ["clang", "-c", "/src/foo.c", "-o", str(build / "foo.c.o")],
        working_dir=build,
    )
    assert result is None


def test_link_command_no_object_inputs_returns_none():
    # Pure -l link with no .o files — not captured as a link command
    build = Path("/build")
    result = LinkCommand.from_arguments(
        ["clang", "-o", str(build / "mybin"), "-lfoo", "-lbar"],
        working_dir=build,
    )
    assert result is None


def test_link_command_versioned_linker_recognized():
    build = Path("/build")
    for linker in ("clang-21", "gcc-13", "ld.bfd", "ld.lld"):
        cmd = LinkCommand.from_arguments(
            [linker, "-o", str(build / "mybin"), str(build / "foo.c.o")],
            working_dir=build,
        )
        assert cmd is not None, f"{linker} should be recognized as a linker"


def test_resolve_targets_static_archive_traversed():
    # A .a archive in linked_binary_inputs should be transitively traversed
    # just like a .so, as long as it has a corresponding link command.
    build = Path("/build")
    src = Path("/src")

    lib_c_o = build / "lib.c.o"
    libfoo_a = build / "libfoo.a"
    main_c_o = build / "main.c.o"
    mybin = build / "mybin"

    compile_cmds = [
        CompileCommand.from_arguments(
            ["clang", "-c", str(src / "lib.c"), "-o", str(lib_c_o)],
            working_dir=build,
        ),
        CompileCommand.from_arguments(
            ["clang", "-c", str(src / "main.c"), "-o", str(main_c_o)],
            working_dir=build,
        ),
    ]
    link_cmds = [
        LinkCommand.from_arguments(
            ["clang", "-r", "-o", str(libfoo_a), str(lib_c_o)],
            working_dir=build,
        ),
        LinkCommand.from_arguments(
            ["clang", "-o", str(mybin), str(main_c_o), str(libfoo_a)],
            working_dir=build,
        ),
    ]
    db = BuildDatabase(
        compile_commands=[c for c in compile_cmds if c is not None],
        link_commands=[lc for lc in link_cmds if lc is not None],
    )
    targets = db.resolve_targets()
    assert "mybin" in targets
    assert [e.source for e in targets["mybin"].entries] == [src / "main.c", src / "lib.c"]


def test_resolve_targets_external_so_silently_skipped():
    # A .so passed directly to the linker but not produced by any link command
    # in the database (e.g. libssl.so) should be skipped without error.
    build = Path("/build")
    src = Path("/src")

    main_o = build / "main.c.o"
    external_so = Path("/usr/lib/libssl.so")

    compile_cmds = [
        CompileCommand.from_arguments(
            ["clang", "-c", str(src / "main.c"), "-o", str(main_o)],
            working_dir=build,
        ),
    ]
    link_cmds = [
        LinkCommand.from_arguments(
            ["clang", "-o", str(build / "mybin"), str(main_o), str(external_so)],
            working_dir=build,
        ),
    ]
    db = BuildDatabase(
        compile_commands=[c for c in compile_cmds if c is not None],
        link_commands=[lc for lc in link_cmds if lc is not None],
    )
    targets = db.resolve_targets()
    assert "mybin" in targets
    assert [e.source for e in targets["mybin"].entries] == [src / "main.c"]


def test_resolve_targets_versioned_so_traversed():
    # When a library is produced as libfoo.so.1.2.3 but a consuming target links
    # against libfoo.so (the unversioned symlink name), resolve_targets should still
    # traverse the library's sources transitively.
    build = Path("/build")
    src = Path("/src")

    lib_c_o = build / "lib.c.o"
    libfoo_versioned = build / "libfoo.so.1.2.3"  # actual linker output
    libfoo_unversioned = build / "libfoo.so"  # symlink name used by consumer
    main_c_o = build / "main.c.o"

    compile_cmds = [
        CompileCommand.from_arguments(
            ["clang", "-c", str(src / "lib.c"), "-o", str(lib_c_o)],
            working_dir=build,
        ),
        CompileCommand.from_arguments(
            ["clang", "-c", str(src / "main.c"), "-o", str(main_c_o)],
            working_dir=build,
        ),
    ]
    link_cmds = [
        # Produces libfoo.so.1.2.3 — output_path key in binary_source_map
        LinkCommand.from_arguments(
            ["clang", "-shared", "-o", str(libfoo_versioned), str(lib_c_o)],
            working_dir=build,
        ),
        # Links against libfoo.so — unversioned symlink name
        LinkCommand.from_arguments(
            ["clang", "-o", str(build / "driver"), str(main_c_o), str(libfoo_unversioned)],
            working_dir=build,
        ),
    ]
    db = BuildDatabase(
        compile_commands=[c for c in compile_cmds if c is not None],
        link_commands=[lc for lc in link_cmds if lc is not None],
    )
    targets = db.resolve_targets()

    assert "driver" in targets
    assert [e.source for e in targets["driver"].entries] == [src / "main.c", src / "lib.c"]
