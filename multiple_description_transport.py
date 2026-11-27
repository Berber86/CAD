"""OpenAI-compatible, single-call transport for the experimental parser.

Creating a transport does not contact a provider. Each callback makes exactly
one SDK call; it deliberately disables SDK-level retries so the parser's
explicit call budget remains authoritative. The GUI uses it only through
the disabled-by-default experimental workflow.
"""
from __future__ import annotations

import json
import queue
import threading
from typing import Any, Callable, Mapping, Sequence, TypeVar

from multiple_description_errors import ProviderResponseError
from multiple_description_parse import (Attempt, COMPARISON_INSTRUCTION, ImageSizeRejected, PageImage,
                                        decode_json_object, system_instruction)


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
            raise ProviderResponseError('provider_refusal')
        content = message.content
    except (AttributeError, IndexError, TypeError) as exc:
        raise ProviderResponseError('invalid_provider_response') from exc
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get('text'), str):
                parts.append(item['text'])
            elif isinstance(getattr(item, 'text', None), str):
                parts.append(item.text)
        content = ''.join(parts)
    if not isinstance(content, str) or not content:
        raise ProviderResponseError('empty_response')
    return content


def _call(client: Any, model: str, messages: list[dict[str, Any]],
          reasoning_effort: str | None = None,
          *,
          temperature: float | None = 0.0,
          response_format: Mapping[str, Any] | None = None) -> str:
    kwargs: dict[str, Any] = {}
    if temperature is not None:
        kwargs['temperature'] = float(temperature)
    if response_format is not None:
        kwargs['response_format'] = dict(response_format)
    if reasoning_effort:
        kwargs['extra_body'] = {'reasoning_effort': reasoning_effort}
    for _ in range(3):
        try:
            completion = client.chat.completions.create(model=model, messages=messages, **kwargs)
            return _content(completion)
        except Exception as exc:
            # Only a clear HTTP 413 establishes a request-size rejection; generic
            # 400/timeout/network failures must not cause automatic reduction.
            status = getattr(exc, 'status_code', None)
            if status == 413:
                raise ImageSizeRejected(status_code=413) from exc
            msg = str(exc).lower()
            dropped = False
            if isinstance(exc, TypeError) or status in (400, 422):
                if 'extra_body' in kwargs and ('reasoning' in msg or 'extra_body' in msg):
                    kwargs.pop('extra_body', None)
                    dropped = True
                if 'response_format' in kwargs and ('response_format' in msg or 'json_object' in msg or 'structured' in msg):
                    kwargs.pop('response_format', None)
                    dropped = True
                if 'temperature' in kwargs and 'temperature' in msg:
                    kwargs.pop('temperature', None)
                    dropped = True
            if not dropped:
                raise
    raise ProviderResponseError('invalid_provider_response')


_T = TypeVar("_T")


def hedged_call(primary: Callable[..., _T], secondary: Callable[..., _T], *args: Any,
                delay: float = 300.0, notice: Callable[[str], None] | None = None,
                **kwargs: Any) -> _T:
    """Вызов с отложенной параллельной копией у второго провайдера.

    Первый запрос не отменяется. Если он не завершился за ``delay`` секунд,
    тот же запрос уходит второму провайдеру, и используется первый успешный
    ответ. Быстрая ошибка первого провайдера (до порога) не маскируется:
    она сразу возвращается вызывающему, как без страховки.
    """
    results: queue.SimpleQueue[tuple[str, bool, Any]] = queue.SimpleQueue()

    def run(name: str, fn: Callable[..., _T]) -> None:
        try:
            results.put((name, True, fn(*args, **kwargs)))
        except BaseException as exc:  # noqa: BLE001 - результат гонки, не потеря
            results.put((name, False, exc))

    threading.Thread(target=run, args=("primary", primary), daemon=True,
                     name="llm-hedge-primary").start()
    try:
        name, ok, value = results.get(timeout=max(0.0, float(delay)))
        if ok:
            return value
        raise value
    except queue.Empty:
        pass
    if notice is not None:
        try:
            notice("Hydra не ответила за 300 с — параллельно запрашиваю ту же модель у RouterAI; "
                   "использую первый успешный ответ.")
        except Exception:  # noqa: BLE001 - журнал не должен ломать запрос
            pass
    threading.Thread(target=run, args=("secondary", secondary), daemon=True,
                     name="llm-hedge-secondary").start()
    errors: dict[str, BaseException] = {}
    while len(errors) < 2:
        name, ok, value = results.get()
        if ok:
            return value
        errors[name] = value
    raise errors.get("primary") or next(iter(errors.values()))


def make_hedged_sender(primary: Callable[..., str], secondary: Callable[..., str], *,
                       delay: float = 300.0, notice: Callable[[str], None] | None = None):
    """Sender: Hydra grok-4.7, после порога — та же модель у RouterAI."""
    def send(pages: Sequence[PageImage], context: Mapping[str, str | None]) -> str:
        return hedged_call(primary, secondary, pages, context, delay=delay, notice=notice)
    return send


def make_hedged_selector(primary: Callable[..., int], secondary: Callable[..., int], *,
                         delay: float = 300.0, notice: Callable[[str], None] | None = None):
    """Selector: та же отложенная гонка провайдеров, что и у sender."""
    def choose(pages: Sequence[PageImage], attempts: Sequence[Attempt]) -> int:
        return hedged_call(primary, secondary, pages, attempts, delay=delay, notice=notice)
    return choose


def create_client(target: Mapping[str, str], *, timeout: float = 500) -> Any:
    """Construct an SDK client with retries disabled; no network call is made."""
    from openai import OpenAI
    if not target.get('api_key') or not target.get('base_url'):
        raise ValueError('provider endpoint and key are required')
    return OpenAI(api_key=target['api_key'], base_url=target['base_url'],
                  timeout=timeout, max_retries=0,
                  default_headers={'HTTP-Referer': 'https://github.com/Berber86/CAD',
                                   'X-Title': 'Title Survey AI'})


def make_sender(client: Any, model: str, reasoning_effort: str | None = None,
                *, temperature: float | None = 0.0):
    """Return the parser's ``send`` callback, one request per invocation."""
    system = system_instruction()

    def send(pages: Sequence[PageImage], context: Mapping[str, str | None]) -> str:
        # Only the explicitly agreed file/portal lot context is sent, not search
        # answers, OCR, filenames, PDF paths or other external metadata.
        labels = {'filename_lot': context.get('filename_lot'), 'portal_lot': context.get('portal_lot')}
        user = [{'type': 'text', 'text': 'Lot context (not an answer filter): ' +
                 json.dumps(labels, ensure_ascii=False)}, *_images(pages)]
        return _call(
            client,
            model,
            [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
            reasoning_effort,
            temperature=temperature,
            response_format={'type': 'json_object'},
        )
    return send


def make_selector(client: Any, model: str, reasoning_effort: str | None = None,
                  *, temperature: float | None = 0.0):
    """Return an optional one-call tie selector; indices are zero-based."""
    def choose(pages: Sequence[PageImage], attempts: Sequence[Attempt]) -> int:
        variants = [{'index': idx, 'pages': list(attempt.pages),
                     'descriptions': [{'complete': item.complete, 'data': item.data.model_dump(mode='json')}
                                      for item in attempt.items]}
                    for idx, attempt in enumerate(attempts)]
        instruction = COMPARISON_INSTRUCTION
        content = [{'type': 'text', 'text': json.dumps(variants, ensure_ascii=False)}, *_images(pages)]
        raw = _call(
            client,
            model,
            [{'role': 'system', 'content': instruction}, {'role': 'user', 'content': content}],
            reasoning_effort,
            temperature=temperature,
            response_format={'type': 'json_object'},
        )
        answer, _ = decode_json_object(raw, expected_keys=frozenset({'selected'}))
        if not isinstance(answer, dict) or set(answer) != {'selected'}:
            raise ValueError('invalid selection')
        selected = answer['selected']
        if isinstance(selected, bool) or not isinstance(selected, int) or not 0 <= selected < len(attempts):
            raise ValueError('invalid selection')
        return selected
    return choose
