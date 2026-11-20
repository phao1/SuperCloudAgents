"""Shared helpers for optional SQL predicates."""

from dataclasses import dataclass, field


@dataclass
class Conditions:
    clauses: list[str] = field(default_factory=list)
    arguments: list[str | int] = field(default_factory=list)

    def equals(self, column: str, value: str | int | None) -> None:
        if value is not None:
            self.clauses.append(f"{column} = ?")
            self.arguments.append(value)

    def compare(self, column: str, operator: str, value: str | int | None) -> None:
        if value is not None:
            self.clauses.append(f"{column} {operator} ?")
            self.arguments.append(value)

    def where(self) -> str:
        return " WHERE " + " AND ".join(self.clauses) if self.clauses else ""
