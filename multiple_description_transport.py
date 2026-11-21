"""OpenAI-compatible, single-call transport for the experimental parser.

Creating a transport does not contact a provider. Each callback makes exactly
one SDK call; it deliberately disables SDK-level retries so the parser's
explicit call budget remains authoritative. The GUI uses it only through
the disabled-by-default experimental workflow.
"""
from __future__ import annotations

import json
from typing import Any, Mapping, Sequence

from llm_parser import LegalDescription
from multiple_description_parse import Attempt, INSTRUCTION, ImageSizeRejected, PageImage


def _images(pages: Sequence[PageImage]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = []
    for page in pages:
        content.append({"type": "text", "text": f"PDF page {page.number}"})
        content.append({"type": "image_url", "image_url": {"url": page.data_url}})
    return content


def _content(completion: Any) -> str:
    try:
        message = completion.choices[0].message
        if getattr(message, 'refusal', None):
            raise ValueError('provider refusal')
        content = message.content
    except (AttributeError, IndexError, TypeError) as exc:
        raise ValueError('invalid provider response') from exc
    if not isinstance(content, str) or not content:
        raise ValueError('empty provider response')
    return content


def _call(client: Any, model: str, messages: list[dict[str, Any]]) -> str:
    try:
        completion = client.chat.completions.create(model=model, messages=messages)
    except Exception as exc:
        # Only a clear HTTP 413 establishes a request-size rejection; generic
        # 400/timeout/network failures must not cause automatic reduction.
        if getattr(exc, 'status_code', None) == 413:
            raise ImageSizeRejected() from exc
        raise
    return _content(completion)


def create_client(target: Mapping[str, str], *, timeout: float = 180) -> Any:
    """Construct an SDK client with retries disabled; no network call is made."""
    from openai import OpenAI
    if not target.get('api_key') or not target.get('base_url'):
        raise ValueError('provider endpoint and key are required')
    return OpenAI(api_key=target['api_key'], base_url=target['base_url'],
                  timeout=timeout, max_retries=0,
                  default_headers={'HTTP-Referer': 'https://github.com/Berber86/CAD',
                                   'X-Title': 'Title Survey AI'})


def make_sender(client: Any, model: str):
    """Return the parser's ``send`` callback, one request per invocation."""
    schema = json.dumps(LegalDescription.model_json_schema(), ensure_ascii=False, separators=(',', ':'))
    system = (INSTRUCTION + '\nEach item data must conform to this JSON schema: ' + schema +
              '\nUse exactly the keys descriptions and complete at the top level, and exactly '
              'the keys complete and data for each item. Return JSON only.')

    def send(pages: Sequence[PageImage], context: Mapping[str, str | None]) -> str:
        # Only the explicitly agreed file/portal lot context is sent, not search
        # answers, OCR, filenames, PDF paths or other external metadata.
        labels = {'filename_lot': context.get('filename_lot'), 'portal_lot': context.get('portal_lot')}
        user = [{'type': 'text', 'text': 'Lot context (not an answer filter): ' +
                 json.dumps(labels, ensure_ascii=False)}, *_images(pages)]
        return _call(client, model, [{'role': 'system', 'content': system},
                                    {'role': 'user', 'content': user}])
    return send


def make_selector(client: Any, model: str):
    """Return an optional one-call tie selector; indices are zero-based."""
    def choose(pages: Sequence[PageImage], attempts: Sequence[Attempt]) -> int:
        variants = [{'index': idx, 'pages': list(attempt.pages),
                     'descriptions': [{'complete': item.complete, 'data': item.data.model_dump(mode='json')}
                                      for item in attempt.items]}
                    for idx, attempt in enumerate(attempts)]
        instruction = ('Compare the candidate parses against the supplied whole PDF pages. '
                       'Select one candidate only, do not combine them. Each candidate lists the pages '
                       'it originally received. If unable to decide, return selected as null. '
                       'Return only JSON {"selected": <zero-based candidate index or null>}.')
        content = [{'type': 'text', 'text': json.dumps(variants, ensure_ascii=False)}, *_images(pages)]
        raw = _call(client, model, [{'role': 'system', 'content': instruction},
                                    {'role': 'user', 'content': content}])
        answer = json.loads(raw)
        if not isinstance(answer, dict) or set(answer) != {'selected'}:
            raise ValueError('invalid selection')
        selected = answer['selected']
        if isinstance(selected, bool) or not isinstance(selected, int) or not 0 <= selected < len(attempts):
            raise ValueError('invalid selection')
        return selected
    return choose
