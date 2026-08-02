"""
Indexer module for Vault Orchestrator.

Single-owner project index with extension-aware comment stripping.
Per §9.1.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:
    from .vault import Vault


# File type patterns for comment detection
COMMENT_PATTERNS = {
    ".py": re.compile(r"#.*$", re.MULTILINE),
    ".js": re.compile(r"//.*$", re.MULTILINE),
    ".ts": re.compile(r"//.*$", re.MULTILINE),
    ".jsx": re.compile(r"//.*$", re.MULTILINE),
    ".tsx": re.compile(r"//.*$", re.MULTILINE),
    ".c": re.compile(r"//.*$", re.MULTILINE),
    ".cpp": re.compile(r"//.*$", re.MULTILINE),
    ".h": re.compile(r"//.*$", re.MULTILINE),
    ".java": re.compile(r"//.*$", re.MULTILINE),
    ".rs": re.compile(r"//.*$", re.MULTILINE),
    ".go": re.compile(r"//.*$", re.MULTILINE),
    ".sh": re.compile(r"#.*$", re.MULTILINE),
    ".bash": re.compile(r"#.*$", re.MULTILINE),
    ".zsh": re.compile(r"#.*$", re.MULTILINE),
    ".yaml": re.compile(r"#.*$", re.MULTILINE),
    ".yml": re.compile(r"#.*$", re.MULTILINE),
    ".toml": re.compile(r"#.*$", re.MULTILINE),
    ".ini": re.compile(r";.*$", re.MULTILINE),
    ".cfg": re.compile(r"#.*$", re.MULTILINE),
    ".conf": re.compile(r"#.*$", re.MULTILINE),
    ".md": None,  # Markdown: # is heading, not comment (per §9.1)
    ".txt": None,  # Plain text: no comment stripping
}


@dataclass
class FileIndex:
    """Index entry for a single file."""
    path: Path
    relative_path: Path
    extension: str
    content_hash: str
    indexed_at: float
    terms: dict[str, int] = field(default_factory=dict)  # word -> count


@dataclass
class ProjectIndex:
    """
    Index for a single project.
    
    Single owner per §9.1 - keyed by absolute Path.
    """
    project_path: Path
    files: dict[Path, FileIndex] = field(default_factory=dict)
    term_index: dict[str, set[Path]] = field(default_factory=dict)
    last_updated: float = 0
    
    def add_file(self, file_path: Path, content: str, file_hash: str, timestamp: float) -> None:
        """
        Add or update a file in the index.
        
        Extracts terms and updates inverted index.
        """
        relative = file_path.relative_to(self.project_path) if file_path.is_relative_to(self.project_path) else file_path
        extension = file_path.suffix.lower()
        
        # Extract terms
        terms = self._extract_terms(content, extension)
        
        # Create index entry
        index = FileIndex(
            path=file_path,
            relative_path=relative,
            extension=extension,
            content_hash=file_hash,
            indexed_at=timestamp,
            terms=terms,
        )
        
        # Remove old terms
        if file_path in self.files:
            old_terms = self.files[file_path].terms
            for term in old_terms:
                if term in self.term_index:
                    self.term_index[term].discard(file_path)
        
        # Add new terms
        for term, count in terms.items():
            if term not in self.term_index:
                self.term_index[term] = set()
            self.term_index[term].add(file_path)
        
        self.files[file_path] = index
        self.last_updated = timestamp
    
    def remove_file(self, file_path: Path) -> None:
        """Remove a file from the index."""
        if file_path in self.files:
            old_terms = self.files[file_path].terms
            for term in old_terms:
                if term in self.term_index:
                    self.term_index[term].discard(file_path)
            del self.files[file_path]
    
    def _extract_terms(self, content: str, extension: str) -> dict[str, int]:
        """
        Extract searchable terms from content.
        
        Extension-aware: only strips code comments, not markdown headings.
        Per §9.1: indexer must not treat # as comment marker for non-code files.
        """
        # Get pattern for file type
        pattern = COMMENT_PATTERNS.get(extension)
        
        if pattern:
            # Strip comments for code files
            content = pattern.sub("", content)
        
        # Tokenize
        terms = defaultdict(int)
        for word in re.findall(r'\w+', content.lower()):
            if len(word) >= 2:  # Skip single chars
                terms[word] += 1
        
        return dict(terms)
    
    def search(self, query: str, top_n: int = 5) -> list[tuple[Path, float]]:
        """
        Search for files matching query.
        
        Returns list of (file_path, score) sorted by relevance.
        """
        query_terms = set(re.findall(r'\w+', query.lower()))
        if not query_terms:
            return []
        
        scores: dict[Path, float] = defaultdict(float)
        
        for term in query_terms:
            matching_files = self.term_index.get(term, set())
            for file_path in matching_files:
                if file_path in self.files:
                    # Score = sum of term frequencies
                    scores[file_path] += self.files[file_path].terms.get(term, 0)
        
        # Sort by score
        sorted_files = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        
        return sorted_files[:top_n]


class Indexer:
    """
    Project indexer with single-owner per project.
    
    Per §9.1 invariant 7: one indexer per project, keyed by absolute Path.
    """
    
    def __init__(self, vault: "Vault"):
        self._vault = vault
        self._indices: dict[Path, ProjectIndex] = {}
    
    def get_index(self, project_path: Path) -> ProjectIndex:
        """
        Get or create index for a project.
        
        Keyed by resolved absolute Path - ensures exactly one index
        per project, preventing the dual-indexer bug.
        """
        resolved = project_path.resolve()
        
        if resolved not in self._indices:
            self._indices[resolved] = ProjectIndex(project_path=resolved)
        
        return self._indices[resolved]
    
    def index_file(self, project_path: Path, file_path: Path, content: str, file_hash: str) -> None:
        """Index a single file."""
        import hashlib
        import time
        
        # Generate hash if not provided
        if not file_hash:
            file_hash = hashlib.md5(content.encode()).hexdigest()
        
        index = self.get_index(project_path)
        index.add_file(file_path, content, file_hash, time.time())
    
    def remove_file(self, project_path: Path, file_path: Path) -> None:
        """Remove a file from the index."""
        resolved = project_path.resolve()
        if resolved in self._indices:
            self._indices[resolved].remove_file(file_path)
    
    def reindex_project(self, project_path: Path) -> None:
        """Reindex all files in a project."""
        import hashlib
        import time
        
        index = self.get_index(project_path)
        index.files.clear()
        index.term_index.clear()
        
        timestamp = time.time()
        
        for file_path in project_path.rglob("*"):
            if not file_path.is_file():
                continue
            
            # Skip certain directories
            if any(part.startswith('.') for part in file_path.parts):
                continue
            if 'tasks' in file_path.parts:
                continue
            if 'assets' in file_path.parts:
                continue
            
            try:
                content = file_path.read_text(errors='ignore')
                file_hash = hashlib.md5(content.encode()).hexdigest()
                index.add_file(file_path, content, file_hash, timestamp)
            except Exception:
                pass
    
    def search_project(self, project_path: Path, query: str, top_n: int = 5) -> list[tuple[Path, float]]:
        """Search for files in a project."""
        index = self.get_index(project_path)
        return index.search(query, top_n)
    
    def get_context_files(
        self,
        project_path: Path,
        query: str,
        top_n: int = 5,
        max_chars: int = 6000,
    ) -> list[tuple[Path, str]]:
        """
        Get context files for a task.
        
        Returns list of (file_path, content) for the most relevant files,
        truncated to max_chars total.
        """
        results = self.search_project(project_path, query, top_n)
        
        context = []
        total_chars = 0
        
        for file_path, score in results:
            try:
                content = file_path.read_text(errors='ignore')
                # Truncate if needed
                if total_chars + len(content) > max_chars:
                    remaining = max_chars - total_chars
                    if remaining > 100:
                        content = content[:remaining] + "\n... [truncated]"
                    else:
                        break
                
                context.append((file_path, content))
                total_chars += len(content)
                
                if total_chars >= max_chars:
                    break
            except Exception:
                pass
        
        return context


# Global indexer instance - single owner per §9.1
_indexer: Indexer | None = None


def get_indexer(vault: "Vault") -> Indexer:
    """Get the global indexer instance."""
    global _indexer
    if _indexer is None:
        _indexer = Indexer(vault)
    return _indexer
