"""
Self-test for TASK-12: Structured pytest output only.

Tests that verify() parses pytest output into structured format:
- {failed_tests: [{name, message}], first_failure_summary: str}
- No lint/syntax fields

Run with: python -m lean.test_task12_verify
"""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lean.verification import verify, _parse_pytest_output, FailedTest


def test_parse_pytest_output_with_failing_and_passing():
    """Test parsing of pytest output with one failing and one passing test."""
    # Simulated pytest output with one failing, one passing
    pytest_output = """
============================= test session starts ==============================
platform linux -- Python 3.13.0, pytest-8.3.0
collected 2 items

test_example.py::test_passing PASSED                                     [ 50%]
test_example.py::test_failing FAILED                                     [100%]

=================================== FAILURES ===================================
_______________________________ test_failing _________________________________

    def test_failing():
>       assert 1 == 2, "expected 1 to equal 2"
E       AssertionError: expected 1 to equal 2
test_example.py:6: AssertionError
======================== 1 failed, 1 passed in 0.05s ========================
"""
    
    failed_tests, first_summary = _parse_pytest_output(pytest_output)
    
    # Assert exactly one failed test
    assert len(failed_tests) == 1, f"Expected 1 failed test, got {len(failed_tests)}"
    
    # Assert the correct name
    assert "test_failing" in failed_tests[0].name, f"Expected test_failing, got {failed_tests[0].name}"
    
    # Assert the correct message contains the assertion
    assert "expected 1 to equal 2" in failed_tests[0].message, \
        f"Expected assertion message, got {failed_tests[0].message}"
    
    # Assert first failure summary
    assert "test_failing" in first_summary, f"Expected test_failing in summary, got {first_summary}"
    assert "expected 1 to equal 2" in first_summary, f"Expected assertion in summary"
    
    print("PASS: parse_pytest_output correctly extracts failed test info")
    return True


def test_no_lint_syntax_fields():
    """Assert that VerificationResult has no lint/syntax fields."""
    from lean.verification import VerificationResult
    import dataclasses
    
    fields = {f.name for f in dataclasses.fields(VerificationResult)}
    
    # Check no lint/syntax fields
    assert "lint" not in fields, "VerificationResult should not have 'lint' field"
    assert "syntax" not in fields, "VerificationResult should not have 'syntax' field"
    assert "syntax_ok" not in fields, "VerificationResult should not have 'syntax_ok' field"
    
    # Check expected fields exist
    assert "passed" in fields, "VerificationResult should have 'passed' field"
    assert "failed_tests" in fields, "VerificationResult should have 'failed_tests' field"
    assert "first_failure_summary" in fields, "VerificationResult should have 'first_failure_summary' field"
    
    print("PASS: VerificationResult has no lint/syntax fields")
    return True


def test_verify_on_real_fixture():
    """Integration test: create a fixture with failing and passing tests."""
    vault = Path(tempfile.mkdtemp(prefix="task12_test_"))
    passed = True
    
    try:
        # Create project with pyproject.toml
        project_dir = vault / "test_project"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").write_text("[project]\nname = 'test'\nversion = '0.1'\n")
        
        # Create test directory with tests
        test_dir = project_dir / "tests"
        test_dir.mkdir()
        (test_dir / "__init__.py").write_text("")
        
        # Write a passing test
        (test_dir / "test_example.py").write_text('''
def test_passing():
    assert True, "This should pass"

def test_failing():
    assert 1 == 2, "expected 1 to equal 2"
''')
        
        # Run verify
        result = verify(project_dir, timeout=30)
        
        # Assert structure
        if not hasattr(result, 'failed_tests'):
            print("FAIL: VerificationResult missing 'failed_tests' attribute")
            passed = False
        elif not hasattr(result, 'first_failure_summary'):
            print("FAIL: VerificationResult missing 'first_failure_summary' attribute")
            passed = False
        else:
            # Assert exactly one failed test
            if len(result.failed_tests) != 1:
                print(f"FAIL: Expected 1 failed test, got {len(result.failed_tests)}")
                passed = False
            elif "test_failing" not in result.failed_tests[0].name:
                print(f"FAIL: Expected test_failing, got {result.failed_tests[0].name}")
                passed = False
            elif "expected 1 to equal 2" not in result.failed_tests[0].message:
                print(f"FAIL: Expected assertion message, got {result.failed_tests[0].message}")
                passed = False
            elif "test_failing" not in result.first_failure_summary:
                print(f"FAIL: Expected test_failing in summary, got {result.first_failure_summary}")
                passed = False
            else:
                print("PASS: verify() returns structured output with correct failed test")
            
            # Assert no lint/syntax in output
            if "lint" in result.output.lower():
                print("FAIL: output should not contain lint info")
                passed = False
            if "syntax" in result.output.lower():
                print("FAIL: output should not contain syntax info")
                passed = False
    finally:
        shutil.rmtree(vault, ignore_errors=True)
    
    return passed


def run_test() -> bool:
    results = [
        test_parse_pytest_output_with_failing_and_passing(),
        test_no_lint_syntax_fields(),
        test_verify_on_real_fixture(),
    ]
    return all(results)


if __name__ == "__main__":
    ok = run_test()
    print()
    print("TASK-12 verify test:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
