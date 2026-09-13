from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parent.parent


def test_documentation_uses_the_public_description_and_license():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    docs_home = (ROOT / "docs" / "index.md").read_text()
    site_config = (ROOT / "docs-site" / "docusaurus.config.ts").read_text()

    assert project["description"].lower() in docs_home.lower()
    # Docusaurus calls it `tagline`; it is the same string mkdocs called
    # `site_description`, and the property is that it tracks pyproject.
    assert f"tagline: '{project['description']}'" in site_config
    assert project["license"] == "EUPL-1.2"
    assert "European Union Public Licence 1.2" in docs_home
    assert "[MIT]" not in docs_home
