import re
from pathlib import Path

import pytest

from istota.search.links import ROUTE_PATHS, file_link, route


def test_client_and_server_paths_agree():
    source = (Path(__file__).parents[1] / "web/src/lib/search/links.ts").read_text()
    array = re.search(r"ROUTE_PATHS\s*=\s*\[(.*?)\]", source, re.S).group(1)
    assert set(re.findall(r"[\"'](/[^\"']*/)[\"']", array)) == ROUTE_PATHS
    assert route("/chat/", room="room", msg=42)["params"] == {"room": "room", "msg": "42"}
    assert file_link("Users/alice/note.md")["type"] == "file"
    with pytest.raises(ValueError):
        route("https://example.com/")
