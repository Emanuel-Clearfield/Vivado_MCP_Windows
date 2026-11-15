"""The guide resources must exist, be non-empty and be served by the server module."""

import asyncio

from vivado_mcp import server


def test_every_guide_is_readable_and_registered():
    for topic in server._GUIDES:
        text = server._read_guide(topic)
        assert len(text) > 300 and text.lstrip().startswith("#")
        assert f"vivado://guide/{topic}" in server.mcp.resources


def test_vivado_guide_tool():
    listing = asyncio.run(server.vivado_guide())
    assert set(listing["topics"]) == set(server._GUIDES)
    got = asyncio.run(server.vivado_guide("workflow"))
    assert got["status"] == "ok" and "synth_1" in got["text"]
    bad = asyncio.run(server.vivado_guide("nope"))
    assert bad["status"] == "error"
