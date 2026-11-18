from pathlib import Path


def confined(root: Path, path: str | Path) -> Path:
    candidate = Path(path).resolve()
    base = root.resolve()
    if candidate == base or not candidate.is_relative_to(base):
        raise ValueError('路径超出专用缓存目录')
    return candidate
