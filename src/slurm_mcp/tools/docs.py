"""
Cluster documentation reader.

Serves Markdown from two places:
  - builtin_docs/  shipped with the package (generic SLURM guidance)
  - the cluster's docs/ directory next to cluster.toml (site-specific rules)
Cluster docs are listed first and win on name clashes.
"""

from pathlib import Path

from ..config import BUILTIN_DOCS_DIR, MAX_FILE_PREVIEW_LINES, MAX_FILE_SIZE_BYTES, get_settings


def _doc_files() -> list[Path]:
    files: dict[str, Path] = {}
    docs_dir = get_settings().docs_dir
    for directory in (docs_dir, BUILTIN_DOCS_DIR):
        if directory is None or not directory.is_dir():
            continue
        for md_file in sorted(directory.glob("*.md")):
            files.setdefault(md_file.name, md_file)
    return list(files.values())


def _title(content: str, fallback: str) -> str:
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return fallback


def list_docs() -> dict:
    """List all documentation files with their titles."""
    docs = []
    for md_file in _doc_files():
        content = md_file.read_text(encoding="utf-8")
        docs.append(
            {
                "file": md_file.name,
                "title": _title(content, md_file.stem),
                "source": "builtin" if md_file.parent == BUILTIN_DOCS_DIR else "cluster",
            }
        )
    result: dict = {"documents": docs}
    if not any(d["source"] == "cluster" for d in docs):
        result["note"] = (
            "No cluster-specific docs found. Add Markdown files to the docs/ directory "
            "next to cluster.toml."
        )
    return result


def read_doc(filename: str) -> dict:
    """Read one doc; partial names match (e.g. 'slurm' → '02-slurm.md')."""
    files = _doc_files()
    needle = filename.lower()
    target = next((f for f in files if f.name.lower() == needle), None) or next(
        (f for f in files if needle in f.name.lower()), None
    )
    if target is None:
        return {"error": f"Document '{filename}' not found.", "available": [f.name for f in files]}

    content = target.read_text(encoding="utf-8")
    if target.stat().st_size > MAX_FILE_SIZE_BYTES:
        return {
            "file": target.name,
            "warning": "large_file",
            "preview": "\n".join(content.splitlines()[:MAX_FILE_PREVIEW_LINES]),
            "advice": "Use hpc_search_docs to find specific sections.",
        }
    return {"file": target.name, "content": content}


def search_docs(query: str) -> dict:
    """Case-insensitive search across all docs, with a few lines of context per hit."""
    needle = query.lower()
    results = []
    for md_file in _doc_files():
        lines = md_file.read_text(encoding="utf-8").splitlines()
        matches = [
            {"line_number": i + 1, "context": "\n".join(lines[max(0, i - 2) : i + 5])}
            for i, line in enumerate(lines)
            if needle in line.lower()
        ]
        if matches:
            results.append(
                {"file": md_file.name, "match_count": len(matches), "matches": matches[:10]}
            )
    return {"query": query, "total_files_matched": len(results), "results": results}
