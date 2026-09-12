"""Apply the minimal opencode-go session-header fix to the pinned Hindsight 0.9.0 venv.

Two-line-class change mirroring what upstream later shipped natively:
  1. engine/llm_wrapper.py        -> forward llm_default_headers into OpenAICompatibleLLM
  2. engine/providers/openai_compatible_llm.py -> accept + apply them on the OpenAI SDK client

Idempotent. Writes .orig backups next to the originals the first time it runs and
emits a unified diff so the change is reproducible / revertible.
"""
import difflib
import json
import os
import shutil
import sys

SITE = os.environ.get("HS_SITE", r"C:\Users\Fwhne\hindsight\.venv\Lib\site-packages\hindsight_api")
PATCHES = [
    (
        r"engine\llm_wrapper.py",
        """            openai_service_tier=openai_service_tier,
            extra_body=extra_body,
            ollama_num_ctx=ollama_num_ctx,
            timeout=timeout,
        )""",
        """            openai_service_tier=openai_service_tier,
            extra_body=extra_body,
            default_headers=default_headers,
            ollama_num_ctx=ollama_num_ctx,
            timeout=timeout,
        )""",
    ),
    (
        r"engine\providers\openai_compatible_llm.py",
        """        extra_body: dict[str, Any] | None = None,
        *,
        ollama_num_ctx: int | None = None,""",
        """        extra_body: dict[str, Any] | None = None,
        default_headers: dict[str, Any] | None = None,
        *,
        ollama_num_ctx: int | None = None,""",
    ),
    (
        r"engine\providers\openai_compatible_llm.py",
        """        # User-configured extra body params (merged into every API call)
        self._config_extra_body = extra_body or {}""",
        """        # User-configured extra body params (merged into every API call)
        self._config_extra_body = extra_body or {}
        # Operator-configured headers (llm_default_headers) applied to the SDK client,
        # so header-gated relays (e.g. opencode-go's x-opencode-session) receive them.
        self._default_headers: dict[str, Any] = dict(default_headers or {})""",
    ),
    (
        r"engine\providers\openai_compatible_llm.py",
        """        if self.timeout:
            client_kwargs["timeout"] = self.timeout

        self._client = AsyncOpenAI(**client_kwargs)""",
        """        if self.timeout:
            client_kwargs["timeout"] = self.timeout
        if self._default_headers:
            client_kwargs["default_headers"] = dict(self._default_headers)

        self._client = AsyncOpenAI(**client_kwargs)""",
    ),
]

diff_out = []
changed = []
for rel, old, new in PATCHES:
    path = os.path.join(SITE, rel)
    src = open(path, encoding="utf-8").read()
    if new in src and old not in src:
        print(f"[skip] already patched: {rel}")
        continue
    if old not in src:
        print(f"[FAIL] anchor not found in {rel}: {old.splitlines()[0]!r}")
        sys.exit(2)
    bak = path + ".orig-opencode-header"
    if not os.path.exists(bak):
        shutil.copy2(path, bak)
        print(f"[backup] {bak}")
    out = src.replace(old, new, 1)
    open(path, "w", encoding="utf-8", newline="").write(out)
    changed.append(rel)
    diff_out.extend(difflib.unified_diff(src.splitlines(True), out.splitlines(True),
                                         f"a/{rel}", f"b/{rel}"))
    print(f"[patched] {rel}")

if diff_out:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "hindsight-opencode-header.patch"), "w", encoding="utf-8") as f:
        f.writelines(diff_out)
print(json.dumps({"changed": changed}))
