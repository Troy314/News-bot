"""Version + journal des versions d'une API FastAPI : lit CHANGELOG.md (même fichier que le front) et expose `GET /api/version`.

Usage (dans main.py) :
    from .changelog import version_router
    app.include_router(version_router(Path(__file__).resolve().parents[2] / "CHANGELOG.md"))

La version courante est la première version du fichier (`## x.y — date`) : un seul endroit à tenir à jour. Le test
`test_changelog_has_entries` (à copier dans les tests de l'outil) échoue si une version n'a aucune ligne."""
import re
from pathlib import Path

from fastapi import APIRouter

_HEAD = re.compile(r"^##\s+(\d+\.\d+(?:\.\d+)?)\s+[—-]\s+(\d{4}-\d{2}-\d{2})\s*$")


def parse_changelog(text: str) -> list[dict]:
    """[{version, date, items}] dans l'ordre du fichier (la plus récente d'abord)."""
    releases: list[dict] = []
    for line in text.splitlines():
        head = _HEAD.match(line)
        if head:
            releases.append({"version": head.group(1), "date": head.group(2), "items": []})
        elif releases and re.match(r"^-\s+", line):
            releases[-1]["items"].append(re.sub(r"^-\s+", "", line).strip())
        elif releases and releases[-1]["items"] and re.match(r"^\s+\S", line):
            releases[-1]["items"][-1] += " " + line.strip()  # ligne retournée
    return releases


def version_router(changelog_path: Path, requires_login=None) -> APIRouter:
    """`GET /api/version` → {"version": "x.y", "releases": [...]}. Passer `requires_login` (ex. `Depends(current_user)`)
    pour le réserver aux personnes connectées."""
    router = APIRouter()
    deps = [requires_login] if requires_login is not None else []

    @router.get("/api/version", dependencies=deps)
    def version():
        releases = parse_changelog(Path(changelog_path).read_text(encoding="utf-8"))
        return {"version": releases[0]["version"] if releases else "dev", "releases": releases}

    return router
