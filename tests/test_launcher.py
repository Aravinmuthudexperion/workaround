from arc_tower.launcher.envfile import parse_env_text
from arc_tower.launcher.platforms import download_urls


def test_env_parser_handles_comments_quotes_and_equals():
    parsed = parse_env_text('# comment\nA=1\nB="two"\nC=x=y\n\nD=\n')
    assert parsed == {"A": "1", "B": "two", "C": "x=y", "D": ""}


def test_windows_urls_use_windows_archives():
    urls = download_urls("windows", "amd64")
    assert urls["grafana"].endswith(".windows-amd64.zip")
    assert urls["loki"].endswith("loki-windows-amd64.exe.zip")
    assert "windows_amd64" in urls["otelcol"]
