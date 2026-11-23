"""Immutable values shared by the URL source onboarding adapters."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from types import MappingProxyType
from typing import Any

_CONFIG_FILENAMES = (
    "current-weekly-portfolio.json",
    "current-weekly-pilot.json",
    "current-weekly-pilot-live.json",
    "current-weekly-live.json",
)


@dataclass(frozen=True)
class OnboardingIssue:
    """The normalized onboarding issue identity and its matching rules."""

    number: str

    def __post_init__(self) -> None:
        value = self.number.strip() if isinstance(self.number, str) else ""
        if re.fullmatch(r"#?[0-9]+", value) is None:
            raise ValueError("onboarding issue must be a numeric issue identifier")
        object.__setattr__(self, "number", str(int(value.lstrip("#"))))

    @classmethod
    def parse(cls, value: str) -> OnboardingIssue:
        return cls(value)

    @property
    def reference(self) -> str:
        return f"#{self.number}"

    def serialize(self) -> str:
        """Return the stable value used in JSON request payloads."""
        return self.number

    def reference_pattern(self) -> re.Pattern[str]:
        return re.compile(rf"(?<![A-Za-z0-9_#]){re.escape(self.reference)}(?![A-Za-z0-9_])")

    def matches(self, text: str) -> bool:
        return self.reference_pattern().search(text) is not None

    def __str__(self) -> str:
        return self.number


@dataclass(frozen=True)
class OnboardingRequest:
    """Immutable operator request passed through the onboarding flow."""

    url: str | None
    probe_evidence: Path | None
    helper_metadata_file: Path | None = None
    helper_metadata_destination: Path | None = None
    feasibility_evidence: Path | None = None
    config_paths: ConfigPaths | None = None
    proposal_file: Path | None = None
    state_dir: Path | None = None
    feasibility_doc: Path | None = None
    source_specific_test: Path | None = None
    onboarding_artifacts: tuple[Path, ...] = ()
    slug: str | None = None
    repo_root: Path | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    reason: str = ""
    proposed_by: str = "agent"
    access_reason: str = ""
    onboarding_issue: str = ""
    run_date: str | None = None
    confirmation_token: str | None = None
    confirmed_by_editor: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        object.__setattr__(self, "onboarding_artifacts", tuple(self.onboarding_artifacts))


@dataclass(frozen=True)
class ConfigPaths:
    """The four canonical weekly configuration destinations."""

    portfolio: Path
    pilot: Path
    pilot_live: Path
    live: Path

    @classmethod
    def from_root(
        cls,
        root: Path,
        *,
        config_dir: Path | None = None,
        portfolio: Path | None = None,
        pilot: Path | None = None,
        pilot_live: Path | None = None,
        live: Path | None = None,
    ) -> ConfigPaths:
        repository = root.resolve()
        directory = config_dir or Path("configs")
        directory = directory if directory.is_absolute() else repository / directory

        def resolve(value: Path | None, filename: str) -> Path:
            selected = value or directory / filename
            return (selected if selected.is_absolute() else repository / selected).resolve()

        return cls(
            resolve(portfolio, _CONFIG_FILENAMES[0]),
            resolve(pilot, _CONFIG_FILENAMES[1]),
            resolve(pilot_live, _CONFIG_FILENAMES[2]),
            resolve(live, _CONFIG_FILENAMES[3]),
        )

    @property
    def all(self) -> tuple[Path, ...]:
        return (self.portfolio, self.pilot, self.pilot_live, self.live)

    def scaffold_kwargs(self) -> dict[str, Path]:
        """Adapt grouped paths at the established scaffold keyword boundary."""
        return {
            "portfolio_config": self.portfolio,
            "pilot_config": self.pilot,
            "pilot_live_config": self.pilot_live,
            "live_config": self.live,
        }
