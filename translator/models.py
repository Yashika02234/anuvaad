"""Translation models with automatic fallback and cheap recovery checks.

A failed model is marked busy and skipped. It is checked again after the wait the
provider asked for, with a tiny probe (Groq: also the tokens left this minute).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Callable

from livekit.agents import APIConnectOptions, llm

logger = logging.getLogger("translator.models")

ATTEMPT_TIMEOUT = 5.0  # seconds without a reply before moving on to the next model
DEFAULT_WAIT = 20.0  # cooldown when the error doesn't say how long to wait
MAX_WAIT = 15 * 60.0  # never wait longer than this between recovery checks
PROBE_MIN_TOKENS = 3500  # one full turn: ~2.9k prompt + 200 reply tokens

_WAIT_RE = re.compile(r"(?:try again in|retry in|retryDelay\W+)\s*([\d.]+(?:ms|h|m|s)(?:[\d.]+(?:ms|h|m|s))*)", re.I)


def parse_duration(text: str | None) -> float | None:
    """'19.9s' / '7m48.288s' / '350ms' / '1h2m' -> seconds."""
    if not text:
        return None
    parts = re.findall(r"([\d.]+)(ms|h|m|s)", text)
    if not parts:
        try:
            return float(text)  # plain "retry-after: 20"
        except ValueError:
            return None
    return sum(float(n) * {"ms": 0.001, "s": 1, "m": 60, "h": 3600}[u] for n, u in parts)


def wait_from_error(err: BaseException) -> float | None:
    """How long the provider asked us to wait, if it said so."""
    headers = getattr(getattr(err, "response", None), "headers", None) or {}
    if headers.get("retry-after"):
        return parse_duration(headers.get("retry-after"))
    m = _WAIT_RE.search(f"{err} {getattr(err, 'body', '')}")
    return parse_duration(m.group(1)) if m else None


class ModelChains:
    """All translation models, a fallback order, and each model's busy / recovery state."""

    def __init__(
        self,
        models: dict[str, llm.LLM],
        groq_ids: dict[str, str] | None = None,
        probe_min_tokens: int = PROBE_MIN_TOKENS,
    ):
        if not models:
            raise ValueError("no translation model available")
        self.models = models  # name -> LLM, in fallback order
        self.order = list(models)
        self.groq_ids = groq_ids or {}  # name -> Groq model id (for the header-based probe)
        self.probe_min_tokens = probe_min_tokens
        self.busy: set[str] = set()
        self._recovery: dict[str, asyncio.Task] = {}
        self._listeners: list[Callable[[str, bool], None]] = []

    @property
    def names(self) -> list[str]:
        return ["auto", *self.order]

    def primary(self, choice: str) -> str:
        return choice if choice in self.models else self.order[0]

    def order_for(self, choice: str) -> list[str]:
        """The chosen model first, then the others in the usual order."""
        first = self.primary(choice)
        return [first] + [n for n in self.order if n != first]

    def on_change(self, fn: Callable[[str, bool], None]) -> None:
        """fn(name, available): a model became busy, or is available again."""
        self._listeners.append(fn)

    def _notify(self, name: str, available: bool) -> None:
        for fn in self._listeners:
            try:
                fn(name, available)
            except Exception:
                logger.exception("model status listener failed")

    async def stream(self, choice: str, ctx: llm.ChatContext) -> AsyncIterator[tuple[str, llm.ChatChunk]]:
        """Yield (model name, chunk) for one reply, falling back on errors.

        The ACTION/EN header lines are held back until the HINDI line starts (they are never
        spoken anyway). So until Hindi starts, any problem moves on to the next model in the
        same turn: an error, a timeout, or a reply that ends without a Hindi line (cut off by
        the token cap, wrong format). Once Hindi has started, nothing is retried: switching
        would make the voice repeat the sentence.
        """
        from .prompts import ReplyParser

        order = self.order_for(choice)
        candidates = [n for n in order if n not in self.busy] or order  # all busy: try anyway
        last_err: BaseException | None = None
        for name in candidates:
            held: list[llm.ChatChunk] = []  # header chunks, not yet passed on
            reply = ReplyParser()
            live = False  # Hindi (or a skip) has started: no switching after this
            try:
                async with self.models[name].chat(
                    chat_ctx=ctx,
                    conn_options=APIConnectOptions(max_retry=0, timeout=ATTEMPT_TIMEOUT),
                ) as stream:
                    async for chunk in stream:
                        if live:
                            yield name, chunk
                            continue
                        held.append(chunk)
                        if chunk.delta and chunk.delta.content:
                            reply.feed(chunk.delta.content)
                        if reply.header_done or reply.skip:
                            live = True
                            for c in held:
                                yield name, c
                            held = []
            except asyncio.CancelledError:
                raise
            except Exception as e:  # rate limit, timeout, provider error
                last_err = e
                if live:  # Hindi already spoken: switching would repeat the sentence
                    logger.warning("model %s failed after the Hindi started (%s), giving up on this turn",
                                   name, type(e).__name__)
                    self._set_busy(name, e)
                    raise
                logger.warning("model %s failed (%s), trying the next one", name, type(e).__name__)
                self._set_busy(name, e)
                continue

            if not live:  # the reply ended before a Hindi line started
                reply.flush()
                if not (reply.skip or reply.hindi.strip()):
                    # cut off or wrong format: nothing spoken yet, try the next model
                    logger.warning("model %s reply had no Hindi line (cut off / wrong format), "
                                   "trying the next one", name)
                    last_err = RuntimeError(f"{name}: reply without a Hindi line")
                    continue
                for c in held:  # a short complete reply that never hit the "HINDI:" check
                    yield name, c
            if name in self.busy:  # answered although marked busy: it's back
                self._set_available(name)
            return
        raise last_err or RuntimeError("no model answered")

    def _set_busy(self, name: str, err: BaseException) -> None:
        wait = min(wait_from_error(err) or DEFAULT_WAIT, MAX_WAIT)
        if name not in self.busy:
            self.busy.add(name)
            logger.info("model %s busy, next check in %.0fs", name, wait)
            self._notify(name, False)
        task = self._recovery.get(name)
        if task is None or task.done():
            self._recovery[name] = asyncio.create_task(self._recover(name, wait))

    def _set_available(self, name: str) -> None:
        if name in self.busy:
            self.busy.discard(name)
            logger.info("model %s available again", name)
            self._notify(name, True)

    async def _recover(self, name: str, wait: float) -> None:
        """A: wait what the provider asked. B: tiny probe; Groq: also check the token budget."""
        while name in self.busy:
            await asyncio.sleep(max(wait, 1.0))
            try:
                again = await self._probe(name)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                again = min(wait_from_error(e) or DEFAULT_WAIT, MAX_WAIT)
                logger.info("model %s still busy (%s), next check in %.0fs", name, type(e).__name__, again)
            if again is None:
                self._set_available(name)
                return
            wait = again

    async def _probe(self, name: str) -> float | None:
        """None = ready for a full turn; otherwise seconds to wait before checking again."""
        model = self.models[name]
        client = getattr(model, "_client", None)
        if name in self.groq_ids and client is not None and hasattr(client, "chat"):
            kwargs = {"reasoning_effort": "none" if "qwen" in self.groq_ids[name] else "low"}
            raw = await client.chat.completions.with_raw_response.create(
                model=self.groq_ids[name],
                messages=[{"role": "user", "content": "Reply OK"}],
                max_completion_tokens=8,
                **kwargs,
            )
            left = raw.headers.get("x-ratelimit-remaining-tokens")
            if left is not None and int(left) < self.probe_min_tokens:
                reset = parse_duration(raw.headers.get("x-ratelimit-reset-tokens")) or 5.0
                logger.info("model %s: %s tokens left this minute, next check in %.0fs", name, left, reset)
                return reset + 0.5
            return None
        # other providers (Gemini): a tiny request through the plugin itself
        ctx = llm.ChatContext.empty()
        ctx.add_message(role="user", content="Reply OK")
        async with model.chat(chat_ctx=ctx, conn_options=APIConnectOptions(max_retry=0, timeout=ATTEMPT_TIMEOUT)) as s:
            async for _ in s:
                pass
        return None

    async def aclose(self) -> None:
        for t in self._recovery.values():
            t.cancel()
