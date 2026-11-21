"""Lossless, side-effect-free handoff from the experimental worker to a UI.

The current application has one ParseOutcome per PDF and one primary plan.
This adapter keeps both dimensions separate until the GUI can consume them;
it never guesses an outcome for a failed file or joins two descriptions.
It does not start requests, touch files, or update widgets.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from acad_builder import DrawPlan
from llm_parser import ParseOutcome
from multiple_description_drawing import PlannedDescription, SceneResult, _shape_matches
from multiple_description_runtime import BatchResult, FileJob


class InvalidHandoff(ValueError):
    """A cancelled, stale, or internally inconsistent batch must not be shown."""


@dataclass(frozen=True)
class DescriptionView:
    source: Path
    file_index: int
    description_index: int
    ordinal: int
    complete: bool
    outcome: ParseOutcome
    plan: DrawPlan | None
    placement_known: bool | None
    diagnostic: str | None


@dataclass(frozen=True)
class DocumentView:
    source: Path
    is_main: bool
    status: str
    pdf_sha256: str | None
    cached: bool
    complete: bool
    descriptions: tuple[DescriptionView, ...]


@dataclass(frozen=True)
class SceneHandoff:
    documents: tuple[DocumentView, ...]
    scene: SceneResult

    @property
    def descriptions(self) -> tuple[DescriptionView, ...]:
        return tuple(description for file in self.documents for description in file.descriptions)

    @property
    def drawable(self) -> tuple[DescriptionView, ...]:
        return tuple(description for description in self.descriptions if description.plan is not None)

    @property
    def all_processed(self) -> bool:
        """Failures or search-incomplete-with-no-result are not empty documents."""
        return bool(self.documents) and all(doc.status in {'parsed', 'no_candidates'} for doc in self.documents)


def build_handoff(batch: BatchResult, *, expected_jobs: Sequence[FileJob] | None = None) -> SceneHandoff:
    """Validate global source indices and retain every description and failure.

    ``description_index`` is the runtime planner's global index, not a lot ID.
    It includes duplicate and failed-to-plan descriptions. An unavailable plan
    has no geometry but keeps its own outcome for developer diagnostics.
    """
    if batch.cancelled:
        raise InvalidHandoff('cancelled batch')
    if expected_jobs is not None and tuple(file.job for file in batch.files) != tuple(expected_jobs):
        raise InvalidHandoff('stale document jobs')
    if len({file.job.path for file in batch.files}) != len(batch.files):
        raise InvalidHandoff('duplicate document jobs')
    parsed: list[tuple[int, int, object]] = []
    for file_index, file in enumerate(batch.files):
        result = file.result
        if result is None:
            if file.status != 'processing_failed':
                raise InvalidHandoff('missing document result')
            continue
        if result.status != file.status:
            raise InvalidHandoff('document status mismatch')
        if file.status == 'parsed' and (result.parse is None or
                                        (result.parse.selected_attempt is None and
                                         not (result.cached and result.parse.selection == 'cache'))):
            raise InvalidHandoff('parsed document has no selected response')
        if file.status in {'no_candidates', 'search_incomplete', 'images_unavailable'} and result.parse is not None:
            raise InvalidHandoff('unexpected parse result')
        if result.parse is None:
            continue
        for ordinal, item in enumerate(file.result.parse.items, 1):
            parsed.append((file_index, ordinal, item))
    plans: dict[int, PlannedDescription] = {}
    failures: dict[int, str] = {}
    for entry in batch.scene.plans:
        index = entry.description_index
        if index in plans or not 0 <= index < len(parsed):
            raise InvalidHandoff('invalid plan index')
        file = batch.files[parsed[index][0]]
        if entry.source != file.job.path or entry.is_main != file.job.is_main or entry.plan.is_neighbor == entry.is_main:
            raise InvalidHandoff('plan source or role mismatch')
        if not _shape_matches(entry.plan, parsed[index][2]):
            raise InvalidHandoff('plan sides mismatch')
        plans[index] = entry
    for failure in batch.scene.failures:
        index = failure.description_index
        if index in plans or index in failures or not 0 <= index < len(parsed):
            raise InvalidHandoff('invalid failure index')
        if failure.source != batch.files[parsed[index][0]].job.path:
            raise InvalidHandoff('failure source mismatch')
        failures[index] = failure.reason
    if set(plans) | set(failures) != set(range(len(parsed))):
        raise InvalidHandoff('missing planned description')
    descriptions: list[list[DescriptionView]] = [[] for _ in batch.files]
    for index, (file_index, ordinal, item) in enumerate(parsed):
        file = batch.files[file_index]
        entry = plans.get(index)
        outcome = ParseOutcome(data=item.data, source='llm:multi', provider=batch.provider, model=batch.model)
        descriptions[file_index].append(DescriptionView(
            source=file.job.path, file_index=file_index, description_index=index, ordinal=ordinal,
            complete=item.complete, outcome=outcome, plan=entry.plan if entry else None,
            placement_known=entry.placement_known if entry else None,
            diagnostic=entry.diagnostic if entry else failures[index],
        ))
    docs = []
    for index, file in enumerate(batch.files):
        result = file.result
        parse = result.parse if result else None
        # "Complete" describes technical coverage, not accuracy: an AI search
        # with no candidates is not proof that the PDF has no descriptions.
        # A failed or missing result is never a confirmed absence of descriptions.
        complete = bool((parse is not None and parse.complete) or file.status == 'no_candidates')
        digest = result.search.get('pdf_sha256') if result else None
        if result is not None and (not isinstance(digest, str) or len(digest) != 64
                                   or any(c not in '0123456789abcdef' for c in digest)):
            raise InvalidHandoff('invalid document digest')
        docs.append(DocumentView(file.job.path, file.job.is_main, file.status, digest,
                                 bool(result and result.cached), complete, tuple(descriptions[index])))
    return SceneHandoff(tuple(docs), batch.scene)
