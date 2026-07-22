"""Shared compact Rich output primitives for public CLI commands."""

import textwrap

from rich.console import Console


def print_section(console: Console, title: str, value: str) -> None:
    """Render one compact titled text section with the established CLI wrapping."""
    console.print(title, markup=False)
    console.print("-" * len(title), markup=False)
    for line in wrapped_lines(value):
        console.print(line, markup=False)
    console.print()


def print_observations(console: Console, title: str, observations: tuple[str, ...]) -> None:
    """Render a compact titled observation list with the established CLI wrapping."""
    console.print(title, markup=False)
    console.print("-" * len(title), markup=False)
    if observations:
        for observation in observations:
            lines = wrapped_lines(observation)
            console.print(f"• {lines[0]}", markup=False)
            for line in lines[1:]:
                console.print(f"  {line}", markup=False)
    else:
        console.print("Not available", markup=False)
    console.print()


def wrapped_lines(value: str) -> tuple[str, ...]:
    """Wrap output text to the existing terminal report width."""
    cleaned = value.strip() or "Not available"
    return tuple(
        textwrap.wrap(
            cleaned,
            width=96,
            break_long_words=False,
            break_on_hyphens=False,
        )
    )
