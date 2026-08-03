"""Connectivity check for the configured LLM providers.

    python -m scripts.test_provider            # test the provider selected in .env
    python -m scripts.test_provider sanctum    # test a specific provider
    python -m scripts.test_provider --all      # test every configured provider
"""

import logging
import sys

from .api_engine import APIEngine, APIError


def check(engine: APIEngine, name: str) -> bool:
    cfg = engine.get_provider(name)
    print(f"\n=== {name} ===")
    print(f"  base_url : {cfg.base_url}")
    print(f"  model    : {cfg.model or '(server default)'}")
    print(f"  api_key  : {'set' if cfg.api_key else 'MISSING'}")

    models = engine.list_models(name)
    if models:
        print(f"  models   : {len(models)} available, e.g. {', '.join(models[:5])}")
    else:
        print("  models   : /models not available on this provider")

    try:
        reply = engine.test_connection(name)
    except (APIError, ValueError) as exc:
        print(f"  RESULT   : FAILED - {exc}")
        return False
    print(f"  RESULT   : OK - {reply!r}")
    return True


def main() -> int:
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')
    engine = APIEngine()
    args = [a for a in sys.argv[1:] if a != '--all']

    if '--all' in sys.argv:
        names = engine.available_providers()
    elif args:
        names = args
    else:
        names = [engine.provider_name]

    results = [check(engine, name) for name in names]
    print(f"\n{sum(results)}/{len(results)} provider(s) reachable.")
    return 0 if all(results) else 1


if __name__ == '__main__':
    sys.exit(main())
