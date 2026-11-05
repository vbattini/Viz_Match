from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FilterSpec:
    name: str
    values: list[str]


@dataclass(frozen=True)
class FilterSelection:
    name: str
    value: str

    @property
    def key(self) -> str:
        name = " ".join(self.name.split())
        value = " ".join(self.value.split())
        return f"{name}={value}"

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "value": self.value}


@dataclass(frozen=True)
class FilterCombination:
    selections: tuple[FilterSelection, ...]

    @property
    def key(self) -> str:
        return "; ".join(selection.key for selection in self.selections)

    def to_dict(self) -> dict[str, list[dict[str, str]]]:
        return {
            "selections": [
                selection.to_dict() for selection in self.selections
            ]
        }


@dataclass(frozen=True)
class DashboardState:
    dashboard: str
    page: str
    filter: FilterSelection | FilterCombination | None
    screenshot: Path
    extracted: dict[str, Any]
    filter_names: list[str] = field(default_factory=list)
    filter_values: dict[str, list[str]] = field(default_factory=dict)
    visuals: list[dict[str, Any]] = field(default_factory=list)
    filter_options: dict[str, list[str]] = field(default_factory=dict)
    filter_control_types: dict[str, str | None] = field(default_factory=dict)

    @property
    def state_key(self) -> str:
        suffix = self.filter.key if self.filter else "initial"
        return f"{self.page}|{suffix}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["screenshot"] = (
            str(self.screenshot)
            if self.extracted.get("capture_status", "captured") == "captured"
            else None
        )
        if self.filter is not None:
            data["filter"] = self.filter.to_dict()
        return data
