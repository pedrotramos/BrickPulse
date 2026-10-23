"""Copy only resolved UV artifacts into a writable cache (local development)."""
from pathlib import Path
import shutil
import sys

cache = Path(sys.argv[1])
for requirement in Path(sys.argv[2]).read_text().splitlines():
    if "==" not in requirement or requirement.startswith("#"):
        continue
    name, version = requirement.split("==", 1)
    for artifact in (cache / "wheels-v5").glob(f"**/{name}/{version}-*"):
        if not artifact.is_symlink():
            continue
        original = artifact.resolve()
        destination = cache / "archive-v0" / original.name
        if destination.is_symlink():
            destination.unlink()
            shutil.copytree(original, destination)
            print(name, version)
