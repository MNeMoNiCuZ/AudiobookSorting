"""Generic LLM client.

Every provider is described in ``.env`` and talked to over the standard OpenAI
chat-completions wire format (``POST {base_url}/chat/completions`` ->
``choices[0].message.content``).

Adding a provider means adding three keys - no code changes:

    AO_PROVIDERS=sanctum,myserver
    AO_PROVIDER_MYSERVER_BASE_URL=http://localhost:1234/v1
    AO_PROVIDER_MYSERVER_API_KEY=sk-...
    AO_PROVIDER_MYSERVER_MODEL=some-model

Sanctum (see docs/api_sanctum.md) is OpenAI-compatible, so it needs no special
casing beyond the optional ``extra_body`` field used for its ``enable_tools`` /
``skill_categories`` extensions.
"""

from __future__ import annotations

import json
import logging
import random
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .settings import Settings, get_settings

# Providers whose /chat/completions rejects an unknown "seed" field.
_NO_SEED_HINTS = ('anthropic.com', 'mistral.ai')


class APIError(RuntimeError):
    """Raised when a provider call fails after all retries."""


class ProviderConfig:
    """One provider's ``AO_PROVIDER_<NAME>_*`` keys, resolved into usable values."""

    def __init__(self, name: str, fields: Dict[str, str], settings: Settings):
        self.name = name
        self.base_url = fields.get('BASE_URL', '').strip().rstrip('/')
        if not self.base_url:
            raise ValueError(
                f"Provider '{name}' has no base URL. Set AO_PROVIDER_{name.upper()}_BASE_URL "
                f"in .env, or configure it on the Settings page."
            )
        self.api_key = fields.get('API_KEY', '').strip()
        self.model = fields.get('MODEL', '').strip()
        self.auth_style = fields.get('AUTH_STYLE', 'bearer').strip().lower()
        self.supports_json_mode = _as_bool(fields.get('SUPPORTS_JSON_MODE', 'true'))
        self.supports_seed = _as_bool(fields.get('SUPPORTS_SEED', 'auto'), default=None)
        self.temperature = settings.get_float('AO_TEMPERATURE', 0.1)
        self.max_tokens = settings.get_int('AO_MAX_TOKENS', 2048)
        self.timeout = settings.get_int('AO_TIMEOUT', 120)
        self.max_retries = settings.get_int('AO_MAX_RETRIES', 3)
        self.extra_body = _as_json(fields.get('EXTRA_BODY'), name)

        if self.supports_seed is None:
            self.supports_seed = not any(h in self.base_url for h in _NO_SEED_HINTS)

    def headers(self) -> Dict[str, str]:
        headers = {'Content-Type': 'application/json'}
        if not self.api_key:
            return headers
        if self.auth_style == 'x-api-key':
            headers['x-api-key'] = self.api_key
        elif self.auth_style == 'none':
            pass
        else:
            headers['Authorization'] = f'Bearer {self.api_key}'
        return headers

    def url(self, path: str) -> str:
        return f"{self.base_url}/{path.lstrip('/')}"

    def __repr__(self) -> str:
        return f"<ProviderConfig {self.name} {self.base_url} model={self.model!r}>"


class APIEngine:
    """Talks to any OpenAI-compatible chat endpoint configured in config.ini."""

    def __init__(self, provider: Optional[str] = None, settings: Optional[Settings] = None):
        self.logger = logging.getLogger(__name__)
        self.settings = settings or get_settings()
        self.provider_name = provider or self.settings.get('AO_PROVIDER')
        if not self.provider_name:
            raise ValueError("No provider selected. Set AO_PROVIDER in .env")
        self.provider = self.get_provider(self.provider_name)

    # ------------------------------------------------------------------ config

    def available_providers(self) -> List[str]:
        return self.settings.provider_names()

    def get_provider(self, name: str) -> ProviderConfig:
        fields = self.settings.provider(name)
        if not fields:
            raise ValueError(
                f"Unknown provider '{name}'. Configured providers: "
                f"{', '.join(self.available_providers()) or '(none)'}"
            )
        return ProviderConfig(name, fields, self.settings)

    # ------------------------------------------------------------------- calls

    def call_api(self, prompt: Dict[str, Any], provider: Optional[str] = None,
                 model: Optional[str] = None, response_format: Optional[Dict] = None,
                 seed: Optional[int] = None) -> str:
        """Send a chat completion and return the assistant text.

        Args:
            prompt: ``{"messages": [...], "temperature": ..., "max_tokens": ...}``.
            provider: Optional provider name overriding the configured one.
            model: Optional model id overriding the provider default.
            response_format: e.g. ``{"type": "json_object"}``; dropped for providers
                configured with ``supports_json_mode = false``.
            seed: Optional determinism seed; a random one is used when omitted.
        """
        cfg = self.get_provider(provider) if provider else self.provider
        messages = prompt.get('messages')
        if not messages:
            raise ValueError("prompt must contain a non-empty 'messages' list")

        body: Dict[str, Any] = {
            'model': model or cfg.model,
            'messages': messages,
            'temperature': prompt.get('temperature', cfg.temperature),
            'max_tokens': prompt.get('max_tokens', cfg.max_tokens),
        }
        if not body['model']:
            body.pop('model')  # let the server pick its own default (Sanctum supports this)
        if 'top_p' in prompt:
            body['top_p'] = prompt['top_p']

        fmt = response_format or prompt.get('response_format')
        if fmt and cfg.supports_json_mode:
            body['response_format'] = fmt

        if cfg.supports_seed:
            body['seed'] = seed if seed is not None else random.randint(0, 2 ** 32 - 1)

        body.update(cfg.extra_body)

        data = self._post_json(cfg, 'chat/completions', body)
        return self._extract_text(data)

    def list_models(self, provider: Optional[str] = None) -> List[str]:
        """Return the model ids the provider advertises on ``GET /models``.

        Returns an empty list if the provider does not implement the endpoint.
        """
        return [m['id'] for m in self.list_models_detailed(provider)]

    def list_models_detailed(self, provider: Optional[str] = None) -> List[Dict[str, Any]]:
        """``GET /models``, keeping the fields a picker wants to show.

        Sanctum (docs/api_sanctum.md) returns collections alongside real models and
        states the response order is display order, so it is never re-sorted here.
        Each item is ``{id, label, description, is_collection, member_count, members,
        supports_tools}`` with sane fallbacks for plain OpenAI-shaped servers.
        """
        cfg = self.get_provider(provider) if provider else self.provider
        try:
            data = self._get_json(cfg, 'models')
        except APIError as exc:
            self.logger.warning("Could not list models for %s: %s", cfg.name, exc)
            return []

        models: List[Dict[str, Any]] = []
        for item in data.get('data', []):
            if not isinstance(item, dict) or not item.get('id'):
                continue
            models.append({
                'id': str(item['id']),
                'label': str(item.get('label') or item['id']),
                'description': str(item.get('description') or ''),
                'is_collection': bool(item.get('is_collection', False)),
                # Absent on non-Sanctum servers; None means "unknown", not "empty".
                'member_count': item.get('member_count'),
                'members': list(item.get('members') or []),
                'supports_tools': item.get('supports_tools'),
            })
        return models

    def test_connection(self, provider: Optional[str] = None) -> str:
        """Round-trip a one-line prompt. Returns the reply, raises on failure."""
        cfg = self.get_provider(provider) if provider else self.provider
        return self.call_api(
            {'messages': [{'role': 'user', 'content': 'Reply with the single word: OK'}],
             'max_tokens': 16, 'temperature': 0.0},
            provider=cfg.name,
        )

    def transcribe_audio(self, file_path: str, model: str = 'whisper-large-v3',
                         language: Optional[str] = None,
                         provider: Optional[str] = None) -> str:
        """Transcribe audio via the OpenAI-shaped ``/audio/transcriptions`` endpoint."""
        import requests

        cfg = self.get_provider(provider) if provider else self.provider
        headers = {k: v for k, v in cfg.headers().items() if k != 'Content-Type'}
        payload = {'model': model}
        if language:
            payload['language'] = language
        with open(file_path, 'rb') as fh:
            response = requests.post(cfg.url('audio/transcriptions'), headers=headers,
                                     data=payload, files={'file': (Path(file_path).name, fh)},
                                     timeout=cfg.timeout)
        if response.status_code != 200:
            raise APIError(f"{cfg.name} transcription failed ({response.status_code}): "
                           f"{response.text[:500]}")
        return response.json().get('text', '')

    # ------------------------------------------------------------------ plumbing

    def _post_json(self, cfg: ProviderConfig, path: str, body: Dict) -> Dict:
        import requests

        url = cfg.url(path)
        last_error = ''
        for attempt in range(1, cfg.max_retries + 1):
            try:
                response = requests.post(url, headers=cfg.headers(), json=body,
                                         timeout=cfg.timeout)
            except requests.RequestException as exc:
                last_error = f"request failed: {exc}"
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise APIError(f"{cfg.name} returned non-JSON body: {exc}") from exc
                last_error = f"HTTP {response.status_code}: {response.text[:500]}"
                # 4xx other than rate-limiting will not get better by retrying.
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    break

            if attempt < cfg.max_retries:
                backoff = 2 ** (attempt - 1)
                self.logger.warning("%s call failed (attempt %d/%d), retrying in %ds: %s",
                                    cfg.name, attempt, cfg.max_retries, backoff, last_error)
                time.sleep(backoff)

        raise APIError(f"Call to {cfg.name} ({url}) failed - {last_error}")

    def _get_json(self, cfg: ProviderConfig, path: str) -> Dict:
        import requests

        try:
            response = requests.get(cfg.url(path), headers=cfg.headers(), timeout=cfg.timeout)
        except requests.RequestException as exc:
            raise APIError(f"request failed: {exc}") from exc
        if response.status_code != 200:
            raise APIError(f"HTTP {response.status_code}: {response.text[:500]}")
        return response.json()

    @staticmethod
    def _extract_text(data: Dict) -> str:
        """Pull the assistant text out of a standard chat-completions response."""
        choices = data.get('choices')
        if not choices:
            raise APIError(f"Response contained no choices: {json.dumps(data)[:500]}")
        message = choices[0].get('message', {})
        content = message.get('content')
        if isinstance(content, list):
            # Some gateways return Anthropic-style content blocks.
            content = ''.join(b.get('text', '') for b in content if isinstance(b, dict))
        if content:
            return content.strip()
        if message.get('refusal'):
            return str(message['refusal']).strip()
        raise APIError(f"Response contained no content: {json.dumps(data)[:500]}")


# ---------------------------------------------------------------- config helpers

def _as_bool(value: Optional[str], default: Optional[bool] = True) -> Optional[bool]:
    if value is None:
        return default
    value = value.strip().lower()
    if value in ('true', 'yes', '1', 'on'):
        return True
    if value in ('false', 'no', '0', 'off'):
        return False
    return default  # 'auto' or anything unrecognised


def _as_json(value: Optional[str], provider_name: str) -> Dict:
    if not value or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Provider '{provider_name}' has invalid extra_body JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"Provider '{provider_name}' extra_body must be a JSON object")
    return parsed
