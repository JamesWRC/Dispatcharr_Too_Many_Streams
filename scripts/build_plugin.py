#!/usr/bin/env python3
"""Build an installable plugin ZIP from an explicit, reproducible source list."""
import argparse
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
TOP_LEVEL = (
    'plugin.py', 'plugin.json', '__init__.py', 'requirements.txt', 'LICENSE', 'README.md',
)
DOCUMENTATION_IMAGES = (
    'img/m3u.png', 'img/TooManyStreamsExample.png', 'img/TooManyStreamsExampleStream.png',
)


def build(output: Path, root: Path = ROOT) -> list[str]:
    """Never include caches, logs, local settings, or arbitrary runtime images."""
    files = [root / name for name in (*TOP_LEVEL, *DOCUMENTATION_IMAGES)]
    files.extend(sorted((root / 'src').rglob('*.py')))
    files = [p for p in files if '__pycache__' not in p.parts]
    for path in files:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f'Missing or unsafe distribution source: {path.name}')
    output.parent.mkdir(parents=True, exist_ok=True)
    members = []
    with ZipFile(output, 'w', compression=ZIP_DEFLATED) as archive:
        for path in sorted(files):
            name = 'too_many_streams/' + path.relative_to(root).as_posix()
            info = ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, path.read_bytes())
            members.append(name)
    return members


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'dist' / 'too_many_streams.zip')
    args = parser.parse_args()
    members = build(args.output)
    print(f'Built {args.output.name}: {len(members)} files')


if __name__ == '__main__':
    main()
