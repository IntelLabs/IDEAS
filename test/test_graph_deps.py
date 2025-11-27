#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#


from clang.cindex import Cursor

from ideas.utils import Symbol, get_all_deps


def test_get_all_deps():
    complete_graph = {
        "A": [
            Symbol(name="B", cursor=Cursor()),
            Symbol(name="C", cursor=Cursor()),
            Symbol(name="D", cursor=Cursor()),
            Symbol(name="G", cursor=Cursor()),
        ],
        "B": [Symbol(name="D", cursor=Cursor())],
        "C": [Symbol(name="D", cursor=Cursor()), Symbol(name="E", cursor=Cursor())],
        "D": [],
        "E": [Symbol(name="E", cursor=Cursor()), Symbol(name="F", cursor=Cursor())],
        "F": [],
        "G": [Symbol(name="A", cursor=Cursor())],
        "H": [Symbol(name="H", cursor=Cursor())],
    }

    all_deps_A = get_all_deps(complete_graph, "A")
    all_deps_A_names = sorted([sym.name for sym in all_deps_A])
    assert all_deps_A_names == ["B", "C", "D", "E", "F", "G"]

    all_deps_B = get_all_deps(complete_graph, "B")
    all_deps_B_names = sorted([sym.name for sym in all_deps_B])
    assert all_deps_B_names == ["D"]

    all_deps_C = get_all_deps(complete_graph, "C")
    all_deps_C_names = sorted([sym.name for sym in all_deps_C])
    assert all_deps_C_names == ["D", "E", "F"]

    all_deps_D = get_all_deps(complete_graph, "D")
    all_deps_D_names = sorted([sym.name for sym in all_deps_D])
    assert all_deps_D_names == []

    all_deps_E = get_all_deps(complete_graph, "E")
    all_deps_E_names = sorted([sym.name for sym in all_deps_E])
    assert all_deps_E_names == ["F"]

    all_deps_G = get_all_deps(complete_graph, "G")
    all_deps_G_names = sorted([sym.name for sym in all_deps_G])
    assert all_deps_G_names == ["A", "B", "C", "D", "E", "F"]

    all_deps_H = get_all_deps(complete_graph, "H")
    all_deps_H_names = sorted([sym.name for sym in all_deps_H])
    assert all_deps_H_names == []
