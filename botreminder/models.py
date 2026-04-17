from dataclasses import dataclass
from typing import List, Optional


@dataclass
class ParsedIntent:
    action: str
    title: Optional[str] = None
    kind: str = "event"
    starts_at: Optional[str] = None
    reminders: Optional[List[int]] = None
    repeat_rule: Optional[str] = None
    repeat_until: Optional[str] = None
    assumptions: Optional[List[str]] = None
    needs_time_question: bool = False
    needs_repeat_until_question: bool = False
    original_text: str = ""

@dataclass
class CommandIntent:
    action: str
    confidence: float = 0
    minutes: Optional[int] = None
    starts_at: Optional[str] = None
    question: Optional[str] = None
    assumptions: Optional[List[str]] = None
