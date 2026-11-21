#!/usr/bin/env python3
"""Manual, opt-in decision-model experiment. Not used by the production pipeline.

Each selected page is evaluated independently. Outputs contain only page numbers,
probabilities or generic error statuses; provider bodies and documents are not saved.
"""
from __future__ import annotations

import argparse
import getpass
import json
import sys
import warnings
from pathlib import Path
from typing import Any, Callable

# Also works when launched as ``python tools/jev_page_probe.py``.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import developer_probability as dp  # noqa: E402
import llm_parser as lp  # noqa: E402

QUESTION = (
    "Does this page contain any part of a legal description of a land parcel, "
    "including the beginning or continuation of boundary courses?"
)
PROVIDERS = {
    "routerai": ("https://routerai.ru/api/v1", "typesafe/jev-1.14", "ROUTERAI_API_KEY"),
    "hydra": ("https://api.hydraai.ru/v1", "jev-1.14", "HYDRA_API_KEY"),
}
ROUTERAI_MICROSOFT = "microsoft/microsoft-decision-1.1"


class KeyUnavailable(Exception):
    """Safe message only; no stored token, key or PIN is included."""


def load_provider_key(provider: str) -> str:
    """Use the app's provider-specific settings and unlock in memory only.

    A separate process cannot read the application's RAM-only PIN or unsaved
    entries. The normal config loader reads protected settings and .env, as well
    as an environment variable. Never fall back to a different provider's key.
    """
    try:
        cfg = lp.load_config()
        providers = cfg.get("api", {}).get("providers", {})
        provider_cfg = providers.get(provider, {}) if isinstance(providers, dict) else {}
        raw_key = lp.clean_provider_key(provider, provider_cfg.get("api_key") if isinstance(provider_cfg, dict) else "")
    except Exception as exc:
        raise KeyUnavailable("Cannot read the app settings. No requests sent.") from exc
    if not raw_key:
        raise KeyUnavailable(f"No saved {provider} key found. Save the key in the app first. No requests sent.")
    try:
        key = lp.unprotect_api_key(provider, raw_key, secret="")
        if not key:
            raise KeyUnavailable("The saved provider key is empty. No requests sent.")
        return key
    except lp.KeyProtectionError:
        if not lp.is_protected_key(raw_key):
            raise KeyUnavailable("Cannot unlock the saved key. No requests sent.") from None
    except KeyUnavailable:
        raise
    except Exception:
        raise KeyUnavailable("Cannot unlock the saved key. No requests sent.") from None
    if not sys.stdin.isatty():
        raise KeyUnavailable("Cannot securely prompt for PIN without a terminal. No requests sent.")
    try:
        # Treat getpass's insecure echo fallback as an error before it reads input.
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            pin = getpass.getpass("PIN for the saved key (hidden; not stored): ")
        key = lp.unprotect_api_key(provider, raw_key, secret=pin)
        if not key:
            raise KeyUnavailable("The saved provider key is empty. No requests sent.")
        return key
    except (Exception, KeyboardInterrupt):
        raise KeyUnavailable("Cannot unlock the saved key or securely read PIN. No requests sent.") from None


def probe_pages(
    pages: list[dict[str, Any]], *, provider: str, api_key: str, base_url: str,
    model: str, send: Callable[..., Any],
) -> dict[str, Any]:
    """Evaluate pages separately; unknown or invalid responses stay unknown.

    A page may be a continuation without obvious boundary courses, and the model
    may be wrong. This experiment neither selects a lot nor builds any geometry.
    """
    if provider not in PROVIDERS or not api_key:
        raise ValueError("Provider and key are required")
    if not pages or any(not isinstance(p.get("page"), int) for p in pages):
        raise ValueError("Expected numbered PDF pages")
    results: list[dict[str, Any]] = []
    for page in pages:
        response = dp.run_model(
            model, base_url, api_key, [page], "visual",
            [{"type": "noul", "text": QUESTION}], send=send, provider=provider,
        )
        entries = response.get("entries", [])
        seconds = round(response["seconds"], 3)
        if response.get("error") or len(entries) != 1 or "answer" not in entries[0]:
            error = {"page": page["page"], "status": "error", "probability": None,
                     "seconds": seconds}
            responses = response.get("provider_responses", [])
            if responses and isinstance(responses[-1].get("status"), int):
                status = responses[-1]["status"]
                error["http_status"] = status
                error["error_kind"] = ("invalid_answer" if not response.get("error") else
                                       "invalid_response" if 200 <= status < 300 else "http_error")
            elif response.get("error", "").startswith("Превышено время ожидания "):
                error["error_kind"] = "timeout"
            elif response.get("error", "").startswith("Ошибка соединения с "):
                error["error_kind"] = "network_error"
            else:
                error["error_kind"] = "processing_error" if response.get("error") else "invalid_answer"
            results.append(error)
            continue
        value = entries[0]["answer"].get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            results.append({"page": page["page"], "status": "error", "probability": None,
                            "seconds": seconds, "error_kind": "invalid_answer"})
            continue
        results.append({"page": page["page"], "status": "ok", "probability": value,
                        "seconds": seconds})
    return {
        "provider": provider,
        "model": model,
        "method": "independent_page_requests",
        "all_selected_pages_succeeded": all(r["status"] == "ok" for r in results),
        "pages": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="PDF to send; contents are transmitted only with --live")
    parser.add_argument("--provider", choices=tuple(PROVIDERS), default="routerai")
    parser.add_argument("--model", choices=("both", "jev", "microsoft"), default=None,
                        help="RouterAI: both (default), jev, microsoft; Hydra: jev only")
    parser.add_argument("--pages", default="", help="PDF pages e.g. 1-3,5; default: all")
    parser.add_argument("--image-side", type=int, default=dp.PDF_IMAGE_MAX_SIDE,
                        help="maximum PDF image side in pixels (96–1568)")
    parser.add_argument("--live", action="store_true", help="explicitly enable paid remote requests")
    args = parser.parse_args(argv)
    if args.pdf.suffix.lower() != ".pdf":
        parser.error("expected a PDF")
    base_url, jev_model, _ = PROVIDERS[args.provider]
    selection = args.model or ("both" if args.provider == "routerai" else "jev")
    if args.provider == "hydra" and selection != "jev":
        parser.error("Microsoft comparison requires RouterAI; Hydra supports only --model jev here")
    models = ([jev_model, ROUTERAI_MICROSOFT] if selection == "both" else
              [jev_model if selection == "jev" else ROUTERAI_MICROSOFT])
    key = ""
    if args.live:
        try:
            key = load_provider_key(args.provider)
        except KeyUnavailable as exc:
            print(str(exc), file=sys.stderr)
            return 2
    try:
        pages = dp.prepare_documents([(str(args.pdf), args.pages)], "visual",
                                     provider=args.provider, pdf_image_side=args.image_side)
    except Exception:
        print("Cannot prepare PDF; check the path, page selection and image size. No requests sent.", file=sys.stderr)
        return 2
    if not args.live:
        print(json.dumps({"mode": "dry-run", "provider": args.provider, "models": models,
                          "pages": [p["page"] for p in pages],
                          "planned_requests_per_model": len(pages),
                          "planned_requests": len(pages) * len(models),
                          "message": "No API calls were made. --live sends page images to the selected provider."},
                         ensure_ascii=False, indent=2))
        return 0
    send = dp.post_routerai if args.provider == "routerai" else dp.post_systemone
    results = [probe_pages(pages, provider=args.provider, api_key=key,
                           base_url=base_url, model=model, send=send) for model in models]
    print(json.dumps({"mode": "live", "results": results}, ensure_ascii=False, indent=2))
    return 0 if all(result["all_selected_pages_succeeded"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
