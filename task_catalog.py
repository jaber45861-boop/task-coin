"""
Secure Task Catalog — read-only boundary for Mini App consumption.

Exposes the active task catalog as safe, immutable TaskSummary objects.
Contains NO write logic, NO business rules, NO database connections.

Public API:
    TaskCatalog.list_available_tasks() -> list[TaskSummary]
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import List

import db


@dataclass(frozen=True)
class TaskSummary:
    """Immutable read-only representation of a task for external consumption.

    Exposes ONLY: id, title, description, type, reward.
    Does NOT expose: task_data, expected_data, active, created_at,
    database connections, user state, or verifier internals.
    """
    id: int
    title: str
    description: str
    type: str
    reward: int


class TaskCatalog:
    """Read-only service that exposes the active task catalog.

    This class must NOT:
        - create, update, or delete tasks
        - modify user_tasks, users, or balances
        - complete tasks or grant rewards
        - open sqlite3 connections directly
        - expose task_data, expected_data, active, or created_at
    """

    def list_available_tasks(self) -> List[TaskSummary]:
        """Return all active tasks as safe, immutable TaskSummary objects.

        Uses the existing db.list_tasks(active_only=True) API.
        Preserves database ordering.
        """
        rows = db.list_tasks(active_only=True)
        return [
            TaskSummary(
                id=row["id"],
                title=row["title"],
                description=row["description"],
                type=row["type"],
                reward=row["reward"],
            )
            for row in rows
        ]
