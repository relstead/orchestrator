"""
Self-test for TASK-13: Observation cache.

Tests that:
1. Cache hit on repeated identical lookup
2. Cache miss after file modification (invalidation)

Run with: python -m lean.test_task13_cache
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lean.indexer import Indexer, FileEntry
from lean.orchestrator import ObservationCache


def test_cache_hit():
    """Test cache hit on repeated identical lookup."""
    # Create a mock indexer
    class MockIndexer:
        def __init__(self):
            self.call_count = 0
        
        def find_references(self, symbol):
            self.call_count += 1
            return [FileEntry(
                path=f"file_{symbol}.py",
                size=100,
                language="python",
                symbols=[symbol],
                line_count=10
            )]
    
    indexer = MockIndexer()
    cache = ObservationCache(indexer)
    
    # First call - should call the indexer
    result1 = cache.find_references("Foo")
    assert indexer.call_count == 1, f"Expected 1 call, got {indexer.call_count}"
    
    # Second call - should be cached (no additional indexer call)
    result2 = cache.find_references("Foo")
    assert indexer.call_count == 1, f"Expected still 1 call (cached), got {indexer.call_count}"
    
    # Results should be identical
    assert len(result1) == len(result2) == 1
    assert result1[0].path == result2[0].path == "file_Foo.py"
    
    print("PASS: cache hit returns cached result without re-scan")
    return True


def test_cache_miss_after_invalidation():
    """Test cache miss after file modification."""
    # Create a mock indexer
    class MockIndexer:
        def __init__(self):
            self.call_count = 0
        
        def find_references(self, symbol):
            self.call_count += 1
            return [FileEntry(
                path=f"file_{symbol}.py",
                size=100,
                language="python",
                symbols=[symbol],
                line_count=10
            )]
    
    indexer = MockIndexer()
    cache = ObservationCache(indexer)
    
    # First call
    result1 = cache.find_references("Foo")
    assert indexer.call_count == 1
    
    # Second call - cached
    result2 = cache.find_references("Foo")
    assert indexer.call_count == 1
    
    # Invalidate file that was in the cached result
    cache.invalidate_file("file_Foo.py")
    
    # Third call - should re-scan
    result3 = cache.find_references("Foo")
    assert indexer.call_count == 2, f"Expected 2 calls (after invalidation), got {indexer.call_count}"
    
    print("PASS: cache miss after invalidation triggers re-scan")
    return True


def test_cache_clear():
    """Test cache clear."""
    class MockIndexer:
        def __init__(self):
            self.call_count = 0
        
        def find_references(self, symbol):
            self.call_count += 1
            return [FileEntry(
                path=f"file_{symbol}.py",
                size=100,
                language="python",
                symbols=[symbol],
                line_count=10
            )]
    
    indexer = MockIndexer()
    cache = ObservationCache(indexer)
    
    # First call
    cache.find_references("Foo")
    assert indexer.call_count == 1
    
    # Clear cache
    cache.clear()
    
    # Second call - should re-scan
    cache.find_references("Foo")
    assert indexer.call_count == 2
    
    print("PASS: cache clear triggers re-scan")
    return True


def test_find_definition_and_importers():
    """Test caching for find_definition and find_importers."""
    class MockIndexer:
        def __init__(self):
            self.call_count = 0
        
        def find_definition(self, symbol):
            self.call_count += 1
            return [FileEntry(
                path=f"def_{symbol}.py",
                size=100,
                language="python",
                symbols=[symbol],
                line_count=10
            )]
        
        def find_importers(self, module):
            self.call_count += 1
            return [FileEntry(
                path=f"import_{module}.py",
                size=100,
                language="python",
                symbols=[],
                line_count=10
            )]
    
    indexer = MockIndexer()
    cache = ObservationCache(indexer)
    
    # Test find_definition
    cache.find_definition("Bar")
    assert indexer.call_count == 1
    cache.find_definition("Bar")  # Cached
    assert indexer.call_count == 1
    
    # Test find_importers
    cache.find_importers("os")
    assert indexer.call_count == 2
    cache.find_importers("os")  # Cached
    assert indexer.call_count == 2
    
    print("PASS: find_definition and find_importers are cached")
    return True


def run_test() -> bool:
    results = [
        test_cache_hit(),
        test_cache_miss_after_invalidation(),
        test_cache_clear(),
        test_find_definition_and_importers(),
    ]
    return all(results)


if __name__ == "__main__":
    ok = run_test()
    print()
    print("TASK-13 observation cache test:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
