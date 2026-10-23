"""Keep the approved original logo distinct from rejected design studies."""

from pathlib import Path
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]


def test_favicon_is_exact_original_frequency_candidate():
    approved = ROOT / "frontend/logo-options/svg/03-frequencia-symbol-color.svg"
    favicon = ROOT / "frontend/public/favicon.svg"
    assert favicon.read_bytes() == approved.read_bytes()
    tree = ET.fromstring(favicon.read_text())
    ns = {"svg": "http://www.w3.org/2000/svg"}
    assert len(tree.findall(".//svg:rect", ns)) == 5
    assert not tree.findall(".//svg:path", ns)
    assert tree.find("svg:g", ns).attrib["fill"] == "#b94b2c"


def test_sidebar_and_browser_use_same_versioned_logo():
    icon_url = "/favicon.svg?v=frequency"
    assert f'src="{icon_url}"' in (ROOT / "frontend/src/app/App.tsx").read_text()
    assert f'href="{icon_url}"' in (ROOT / "frontend/index.html").read_text()
