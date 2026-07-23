"""Code indexing for deterministic relevance search.

No AI involved - pure text matching to find relevant files.
Enhanced with jedi for Python symbol/import analysis.
"""

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


@dataclass
class FileEntry:
    """Indexed file metadata."""
    path: str
    size: int
    language: str | None
    symbols: list[str]
    line_count: int
    imports: list[str] = field(default_factory=list)  # Python imports
    references: dict[str, list[str]] = field(default_factory=dict)  # symbol -> [files that reference it]


# Code extensions
CODE_EXTENSIONS = {".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".go", ".rs", ".rb", ".php"}
SKIP_EXTENSIONS = {".png", ".jpg", ".gif", ".pdf", ".zip", ".exe", ".pyc", ".mp3", ".mp4"}

# Try to import jedi for enhanced Python analysis
try:
    import jedi
    JEDI_AVAILABLE = True
except ImportError:
    JEDI_AVAILABLE = False


class Indexer:
    """Indexes a project for fast relevance search."""
    
    def __init__(self, project_path: Path):
        self.project_path = project_path
        self.files: dict[str, FileEntry] = {}
        self.keywords: dict[str, set[str]] = {}
        self._symbol_index: dict[str, set[str]] = {}  # symbol name -> files

    def build(self) -> None:
        """Build index of all files in project."""
        self.files.clear()
        self.keywords.clear()
        self._symbol_index.clear()
        
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
        
        # Build cross-reference index
        self._build_references()

    def _index_file(self, path: Path) -> None:
        """Index a single file."""
        rel = str(path.relative_to(self.project_path))
        
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return
        
        lines = content.split("\n")
        lang = self._detect_language(path)
        symbols, imports = self._extract_symbols_and_imports(content, lang, path)
        
        entry = FileEntry(
            path=rel,
            size=len(content),
            language=lang,
            symbols=symbols,
            line_count=len(lines),
            imports=imports,
        )
        
        self.files[rel] = entry
        
        # Index keywords
        words = self._extract_words(content)
        for word in words:
            if word not in self.keywords:
                self.keywords[word] = set()
            self.keywords[word].add(rel)
        
        # Index symbols for fast lookup
        for sym in symbols:
            if sym not in self._symbol_index:
                self._symbol_index[sym] = set()
            self._symbol_index[sym].add(rel)

    def _build_references(self) -> None:
        """Build cross-reference index for symbol references."""
        for path, entry in self.files.items():
            entry.references = {}
            for sym in entry.symbols:
                entry.references[sym] = []

    def _detect_language(self, path: Path) -> str | None:
        """Detect language from extension."""
        ext = path.suffix.lower()
        return {".py": "python", ".js": "javascript", ".ts": "typescript",
                ".go": "go", ".rs": "rust", ".java": "java"}.get(ext)

    def _extract_symbols_and_imports(self, content: str, lang: str | None, path: Path) -> tuple[list[str], list[str]]:
        """Extract class/function names and imports.
        
        Uses jedi for Python if available, falls back to regex.
        """
        imports = []
        
        if lang == "python":
            # Try jedi first for accurate analysis
            if JEDI_AVAILABLE:
                try:
                    script = jedi.Script(content, path=str(path))
                    definitions = script.get_names(all_scopes=True)
                    
                    symbols = []
                    for d in definitions:
                        # Only include top-level definitions
                        if d.type in ("class", "function", "async_function"):
                            symbols.append(d.name)
                    
                    # Get imports
                    for d in definitions:
                        if d.type == "import":
                            imports.append(d.name)
                    
                    return list(dict.fromkeys(symbols))[:50], imports[:30]
                except Exception:
                    pass  # Fall back to regex
            
            # Fallback: regex-based extraction
            symbols = re.findall(r"^\s*(?:async\s+)?(?:def|class)\s+(\w+)", content, re.MULTILINE)[:50]
            imports = re.findall(r"^\s*(?:from\s+(\S+)|import\s+(\S+))", content, re.MULTILINE)
            imports = [imp[0] or imp[1] for imp in imports if imp][-30:]
            return symbols, imports
        
        elif lang in ("javascript", "typescript"):
            symbols = re.findall(r"^\s*(?:export\s+)?(?:function|const|class)\s+(\w+)", content, re.MULTILINE)[:50]
            return symbols, []
        
        return [], []

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

    def find_references(self, symbol: str) -> list[FileEntry]:
        """Find all files that define or reference a symbol.
        
        Uses the symbol index for fast lookup.
        """
        if symbol in self._symbol_index:
            files = self._symbol_index[symbol]
            return [self.files[f] for f in files if f in self.files]
        
        # Fall back to keyword search
        return self.find_relevant(symbol, top_n=10)

    def find_definition(self, symbol: str) -> list[FileEntry]:
        """Find files that define a specific symbol.
        
        Returns files that contain a class, function, or variable definition.
        Uses symbol index for fast lookup.
        """
        if symbol in self._symbol_index:
            files = self._symbol_index[symbol]
            return [self.files[f] for f in files if f in self.files]
        
        return []

    def find_importers(self, module: str) -> list[FileEntry]:
        """Find files that import a specific module.
        
        Uses the imports list in FileEntry to find importers.
        """
        importers = []
        for entry in self.files.values():
            if module in entry.imports:
                importers.append(entry)
        return importers

    def find_tests(self, symbol: str) -> list[FileEntry]:
        """Find test files related to a symbol.
        
        Looks for files with 'test' in the path or filename.
        """
        results = self.find_references(symbol)
        test_files = [f for f in results if "test" in f.path.lower() or "_test" in f.path]
        
        # Also scan for test files in test directories
        for path in self.project_path.rglob("*test*.py"):
            rel = str(path.relative_to(self.project_path))
            if rel in self.files:
                test_files.append(self.files[rel])
        
        return test_files

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
