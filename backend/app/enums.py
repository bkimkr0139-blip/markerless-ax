from __future__ import annotations

from enum import StrEnum


class Confidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class Decision(StrEnum):
    PASS = "PASS"
    REVIEW = "REVIEW"
    FAIL = "FAIL"
    RETAKE = "RETAKE"
    INVALID = "INVALID"


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class EvidenceKind(StrEnum):
    IMAGE = "image"
    DEPTH = "depth"
    POINT_CLOUD = "point_cloud"
    WORLDMAP = "worldmap"
    MODEL = "model"
    REPORT = "report"
