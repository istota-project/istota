"""Root stubs at the two module paths a running scheduler spawns by name.

The auto-update cron runs `git reset --hard` minutes before it restarts the
scheduler, and a scheduler started before the move still spawns the tool server
and the OCR leaf on their old paths. Without these, a native task or an OCR call
in that window fails with `No module named ...`.
"""

from __future__ import annotations


def test_the_tool_server_stub_main_is_the_tool_server_main():
    import istota.sandbox.tool_server as server
    import istota.tool_server as stub  # move-modules: keep

    assert stub.main is server.main


def test_the_ocr_leaf_stub_main_is_the_leaf_main():
    import istota.lib.ocr_leaf as leaf
    import istota.ocr_leaf as stub  # move-modules: keep

    assert stub.main is leaf.main
