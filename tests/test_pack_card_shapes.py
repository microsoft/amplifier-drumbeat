"""The loader must read BOTH card shapes, or a card migration is a flag day.

pack-card.v1 declares `tools:` as a list of {name, bin, description} mappings.
Every drumpack shipped today still declares it as a list of bare strings. If
the loader only ever accepted one shape, migrating any single drumpack's card
would crash ALL drumpack loading -- the drumpacks load as a set, so one bad
card takes the whole brain down with it.
"""

import pytest

from drumbeat.packs import PackError, load_pack


def _write_pack(root, tools_block: str):
    pack = root / "demo"
    (pack / "bin").mkdir(parents=True)
    launcher = pack / "bin" / "demo-tool"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    (pack / "drumpack.md").write_text(
        "---\n"
        "pack_format: 1\n"
        "name: demo\n"
        "description: a drumpack that exists to exercise the card shapes\n"
        f"{tools_block}"
        "---\n\n"
        "Usage guidance lives here, free-form, and is contract.\n",
        encoding="utf-8",
    )
    return pack


def test_legacy_string_shape_still_loads(tmp_path):
    pack = _write_pack(tmp_path, "tools:\n  - demo-tool\n")
    assert list(load_pack(pack).tools) == ["demo-tool"]


def test_pack_card_v1_mapping_shape_loads(tmp_path):
    pack = _write_pack(
        tmp_path,
        "tools:\n"
        "  - name: demo-tool\n"
        "    bin: bin/demo-tool\n"
        "    description: does the demo thing\n",
    )
    assert list(load_pack(pack).tools) == ["demo-tool"]


def test_mapping_without_a_name_is_refused_loudly(tmp_path):
    pack = _write_pack(
        tmp_path, "tools:\n  - bin: bin/demo-tool\n    description: nameless\n"
    )
    with pytest.raises(PackError) as err:
        load_pack(pack)
    assert "name" in str(err.value)


def test_a_shape_that_is_neither_is_refused_by_type(tmp_path):
    pack = _write_pack(tmp_path, "tools:\n  - 17\n")
    with pytest.raises(PackError) as err:
        load_pack(pack)
    assert "int" in str(err.value)
