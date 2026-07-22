"""Code indexing for deterministic relevance search.

No AI involved - pure text matching to find relevant files.
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FileEntry:
    """Indexed file metadata."""
    path: str
    size: int
    language: str | None
    symbols: list[str]
    line_count: int


# Code extensions
CODE_EXTENSIONS = {".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs", ".rb", ".php"}
SKIP_EXTENSIONS = {".png", ".jpg", ".gif", ".pdf", ".zip", ".exe", ".pyc", ".mp3", ".mp4"}


class Indexer:
    """Indexes a project for fast relevance search."""
    
    def __init__(self, project_path: Path):
        self.project_path = project_path
        self.files: dict[str, FileEntry] = {}
        self.keywords: dict[str, set[str]] = {}

    def build(self) -> None:
        """Build index of all files in project."""
        self.files.clear()
        self.keywords.clear()
        
        skip_dirs = {"__pycache__", ".git", "node_modules", ".venv", "venv", ".pytest_cache"}
        
        for path in self.project_path.rglob("*"):
            if path.is_dir():
                if any(s in path.parts for s in skip_dirs):
                    continue
                continue
            
            if path.suffix.lower() in SKIP_EXTENSIONS:
                continue
            
            if path.stat().st_size > 100_000:
                continue
            
            self._index_file(path)

    def _index_file(self, path: Path) -> None:
        """Index a single file."""
        rel = str(path.relative_to(self.project_path))
        
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return
        
        lines = content.split("\n")
        lang = self._detect_language(path)
        symbols = self._extract_symbols(content, lang)
        
        entry = FileEntry(
            path=rel,
            size=len(content),
            language=lang,
            symbols=symbols,
            line_count=len(lines),
        )
        
        self.files[rel] = entry
        
        # Index keywords
        words = self._extract_words(content)
        for word in words:
            if word not in self.keywords:
                self.keywords[word] = set()
            self.keywords[word].add(rel)

    def _detect_language(self, path: Path) -> str | None:
        """Detect language from extension."""
        ext = path.suffix.lower()
        return {".py": "python", ".js": "javascript", ".ts": "typescript",
                ".go": "go", ".rs": "rust", ".java": "java"}.get(ext)

    def _extract_symbols(self, content: str, lang: str | None) -> list[str]:
        """Extract class/function names."""
        if lang == "python":
            return re.findall(r"^\s*(?:async\s+)?(?:def|class)\s+(\w+)", content, re.MULTILINE)[:50]
        elif lang in ("javascript", "typescript"):
            return re.findall(r"^\s*(?:export\s+)?(?:function|const|class)\s+(\w+)", content, re.MULTILINE)[:50]
        return []

    def _extract_words(self, content: str) -> set[str]:
        """Extract significant words.
        
        Only strips C-style // comments, preserving markdown headings (#).
        This is intentional - markdown content (TTRPG, worldbuilding, docs)
        uses # for headings which should be indexed.
        """
        # Remove quoted strings first
        content = re.sub(r'["\'].*?["\']', "", content)
        # Only strip // comments, NOT # (markdown headings)
        content = re.sub(r"//.*", "", content)
        words = re.findall(r"\b[a-z_][a-z0-9_]{2,}\b", content.lower())
        stop = {"the", "and", "for", "are", "but", "not", "you", "all", "can", "return", "import", "class", "def", "function", "const"}
        return {w for w in words if w not in stop and len(w) > 3}

    def find_relevant(self, query: str, top_n: int = 5, prefer_languages: list[str] | None = None) -> list[FileEntry]:
        """Find most relevant files for a query."""
        query_words = {w for w in re.findall(r"\b\w+\b", query.lower()) if len(w) > 2}
        
        scores: dict[str, float] = {}
        
        for path, entry in self.files.items():
            if prefer_languages and entry.language not in prefer_languages:
                continue
            
            score = 0.0
            
            # Keyword matches
            for word in query_words:
                if word in self.keywords and path in self.keywords[word]:
                    score += 1.0
            
            # Symbol matches (higher weight)
            for sym in entry.symbols:
                for word in query_words:
                    if word in sym.lower():
                        score += 2.0
            
            # Path matches
            for word in query_words:
                if word in path.lower():
                    score += 1.5
            
            if score > 0:
                scores[path] = score
        
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return [self.files[p] for p, _ in ranked[:top_n]]

    def get_context(self, files: list[FileEntry], max_chars: int = 8000) -> str:
        """Build context string from file list."""
        parts = []
        total = 0
        
        for entry in files:
            path = self.project_path / entry.path
            if not path.exists():
                continue
            
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            
            if total + len(content) > max_chars:
                remaining = max_chars - total
                if remaining > 100:
                    content = content[:remaining] + f"\n... [{len(content) - remaining} more chars]"
                else:
                    break
            
            parts.append(f"\n{'='*60}\nFILE: {entry.path}\n{'='*60}\n{content}")
            total += len(content)
        
        return "\n".join(parts)
