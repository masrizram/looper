"""Prompt templates, one per agent role.

Kept as pure functions of their inputs so they are trivially testable and
carry no hidden state.
"""

from __future__ import annotations

from collections.abc import Sequence

#: Injected into every prompt so a long loop cannot drift the agents off the
#: original goal (context rot / scope creep). ADR-007.
SCOPE_GUARD = (
    "Stay strictly within the goal above. Do NOT add features, refactor "
    "unrelated code, change configuration, install packages, run shell "
    "commands, or touch files outside what this phase produces. If a "
    "request is out of scope, say so and stop. Do not 'hallucinate' changes "
    "to satisfy a test."
)


class PromptGenerator:
    """Builds the user-message text for each phase."""

    @staticmethod
    def research(goal: str) -> str:
        return (
            "You are a senior technical researcher. Research the best practices, "
            "tech stack, libraries, and architecture for this project:\n\n"
            f"{goal}\n\n"
            "Provide: recommended stack, rationale, alternatives, and pitfalls. "
            f"Output as structured markdown.\n\n{SCOPE_GUARD}"
        )

    @staticmethod
    def architecture(goal: str, research: str) -> str:
        return (
            "You are a system architect. Design a complete architecture for:\n\n"
            f"{goal}\n\n"
            f"Based on this research:\n{research}\n\n"
            "Provide: components, data models, API design, security, deployment, "
            f"and folder structure. Output as structured markdown.\n\n{SCOPE_GUARD}"
        )

    @staticmethod
    def api_design(goal: str, architecture: str) -> str:
        return (
            "You are a UX/API designer. Given this system architecture for:\n\n"
            f"{goal}\n\n"
            f"Architecture:\n{architecture}\n\n"
            "Propose intuitive API endpoints (or CLI/UX flow if there's no API), "
            "naming conventions, request/response shapes, and error-handling "
            f"patterns that maximize developer experience. Output as "
            f"structured markdown.\n\n{SCOPE_GUARD}"
        )

    @staticmethod
    def build(goal: str, architecture: str, *, package_mode: bool = False) -> str:
        # In package mode the parser only recognises one marker syntax, so the
        # prompt has to name it exactly; anything else falls back to the
        # single-file path and the multi-file feature silently never fires.
        output_rule = (
            "Output EVERY file using this exact marker syntax, one per file:\n"
            "### FILE: relative/path/to/file.py\n"
            "```python\n"
            "<complete file contents>\n"
            "```\n"
            "Use only relative paths (no leading '/', no '..'). Allowed "
            "extensions: .py .md .txt .toml .cfg .ini .json .yaml .yml."
            if package_mode
            else "Output each file in a separate code block with filename header."
        )
        return (
            "You are an expert code builder. Generate ALL production-ready code "
            f"for:\n\n{goal}\n\n"
            f"Based on this architecture:\n{architecture}\n\n"
            "Rules: NO placeholders, NO TODOs, NO incomplete code. "
            "Every file must be complete and functional. Include tests. "
            f"{output_rule}\n\n{SCOPE_GUARD}"
        )

    @staticmethod
    def test(goal: str, code: str) -> str:
        return (
            "You are a QA engineer. Write comprehensive unit and integration tests "
            f"for:\n\n{goal}\n\n"
            f"Code under test:\n{code}\n\n"
            "Cover: edge cases, error handling, happy paths. Output pytest code "
            f"only, importing from the module under test. The tests must genuinely "
            f"exercise the code's behavior, not hardcode expected results.\n\n{SCOPE_GUARD}"
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
            "(critical/high/medium/low) per finding. Finish with the verdict on "
            "a line of its own, in exactly this format and nowhere else in the "
            "reply:\n"
            "Score: <0-100>\n"
            "Do not write the word 'score' anywhere else -- the number on that "
            f"line is parsed as the build's review score.\n\n{SCOPE_GUARD}"
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
            f"If there are no issues, say 'No issues found.'\n\n{SCOPE_GUARD}"
        )

    @staticmethod
    def performance_optimize(goal: str, code: str) -> str:
        return (
            "You are a performance optimizer. Analyze and refactor this code "
            f"for:\n\n{goal}\n\n"
            f"Code:\n{code}\n\n"
            "Identify time/space complexity issues, unnecessary work, and resource "
            "waste. Provide the optimized code plus a short summary of what changed "
            f"and why.\n\n{SCOPE_GUARD}"
        )

    @staticmethod
    def documentation(goal: str, architecture: str, code: str) -> str:
        return (
            "You are a technical documentation writer. Write documentation "
            f"for:\n\n{goal}\n\n"
            f"Architecture:\n{architecture}\n\n"
            f"Code:\n{code}\n\n"
            "Produce a README with: overview, setup, usage, API/CLI reference, and "
            f"configuration. Output as markdown.\n\n{SCOPE_GUARD}"
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
            f"\n\n{SCOPE_GUARD}"
        )
