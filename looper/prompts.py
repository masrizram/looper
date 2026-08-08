"""Prompt templates, one per agent role.

Kept as pure functions of their inputs so they are trivially testable and
carry no hidden state.
"""

from __future__ import annotations

from collections.abc import Sequence


class PromptGenerator:
    """Builds the user-message text for each phase."""

    @staticmethod
    def research(goal: str) -> str:
        return (
            "You are a senior technical researcher. Research the best practices, "
            "tech stack, libraries, and architecture for this project:\n\n"
            f"{goal}\n\n"
            "Provide: recommended stack, rationale, alternatives, and pitfalls. "
            "Output as structured markdown."
        )

    @staticmethod
    def architecture(goal: str, research: str) -> str:
        return (
            "You are a system architect. Design a complete architecture for:\n\n"
            f"{goal}\n\n"
            f"Based on this research:\n{research}\n\n"
            "Provide: components, data models, API design, security, deployment, "
            "and folder structure. Output as structured markdown."
        )

    @staticmethod
    def api_design(goal: str, architecture: str) -> str:
        return (
            "You are a UX/API designer. Given this system architecture for:\n\n"
            f"{goal}\n\n"
            f"Architecture:\n{architecture}\n\n"
            "Propose intuitive API endpoints (or CLI/UX flow if there's no API), "
            "naming conventions, request/response shapes, and error-handling "
            "patterns that maximize developer experience. Output as structured markdown."
        )

    @staticmethod
    def build(goal: str, architecture: str) -> str:
        return (
            "You are an expert code builder. Generate ALL production-ready code "
            f"for:\n\n{goal}\n\n"
            f"Based on this architecture:\n{architecture}\n\n"
            "Rules: NO placeholders, NO TODOs, NO incomplete code. "
            "Every file must be complete and functional. Include tests. "
            "Output each file in a separate code block with filename header."
        )

    @staticmethod
    def test(goal: str, code: str) -> str:
        return (
            "You are a QA engineer. Write comprehensive unit and integration tests "
            f"for:\n\n{goal}\n\n"
            f"Code under test:\n{code}\n\n"
            "Cover: edge cases, error handling, happy paths. Output pytest code "
            "only, importing from the module under test."
        )

    @staticmethod
    def review(goal: str, code: str) -> str:
        return (
            "You are a senior code reviewer, a separate instance from whoever "
            "wrote this code. Review this code for:\n\n"
            f"{goal}\n\n"
            f"Code:\n{code}\n\n"
            "Check: security vulnerabilities, performance issues, code quality, "
            "best practices, missing tests. Provide a severity rating "
            "(critical/high/medium/low) per finding, and end with a line in the "
            "exact format 'Score: <0-100>'."
        )

    @staticmethod
    def security_audit(goal: str, code: str) -> str:
        return (
            "You are a security auditor. Audit this code for:\n\n"
            f"{goal}\n\n"
            f"Code:\n{code}\n\n"
            "Look for injection flaws, auth/authorization issues, secrets handling, "
            "input validation gaps, and other vulnerabilities. List each finding as "
            "its own bullet starting with the severity, e.g. '- HIGH: <description>'. "
            "If there are no issues, say 'No issues found.'"
        )

    @staticmethod
    def performance_optimize(goal: str, code: str) -> str:
        return (
            "You are a performance optimizer. Analyze and refactor this code "
            f"for:\n\n{goal}\n\n"
            f"Code:\n{code}\n\n"
            "Identify time/space complexity issues, unnecessary work, and resource "
            "waste. Provide the optimized code plus a short summary of what changed "
            "and why."
        )

    @staticmethod
    def documentation(goal: str, architecture: str, code: str) -> str:
        return (
            "You are a technical documentation writer. Write documentation "
            f"for:\n\n{goal}\n\n"
            f"Architecture:\n{architecture}\n\n"
            f"Code:\n{code}\n\n"
            "Produce a README with: overview, setup, usage, API/CLI reference, and "
            "configuration. Output as markdown."
        )

    @staticmethod
    def fix(goal: str, code: str, issues: Sequence[str]) -> str:
        issues_text = "\n".join(f"- {issue}" for issue in issues)
        return (
            "You are an expert fixer. Fix these issues in the project:\n\n"
            f"{goal}\n\n"
            f"Current code:\n{code}\n\n"
            f"Issues to fix:\n{issues_text}\n\n"
            "Provide the corrected, complete code for each affected file."
        )
