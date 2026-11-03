import json

from collection_search import claude_config


def test_install_keeps_other_servers(tmp_path):
    path = tmp_path / "Claude" / "claude_desktop_config.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}, "theme": "dark"}))
    assert not claude_config.is_installed(str(path))
    claude_config.install(str(path))
    data = json.loads(path.read_text())
    assert data["theme"] == "dark" and data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["collection-search"] == claude_config.shim_command()
    assert claude_config.is_installed(str(path))
    assert (tmp_path / "Claude" / "claude_desktop_config.json.bak").exists()


def test_install_creates_file(tmp_path):
    path = tmp_path / "Claude" / "claude_desktop_config.json"
    claude_config.install(str(path))
    assert claude_config.is_installed(str(path))
