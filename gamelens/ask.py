"""Talking to Claude.

Two things matter here. First, the answer is going to be *spoken*, so the
system prompt forbids markdown, lists and preamble - anything that sounds
wrong read aloud. Second, latency: the response is streamed and handed to the
TTS one complete sentence at a time, so speech starts about a second after the
question instead of after the whole answer is written.
"""

from __future__ import annotations

import os
import re
import base64
from typing import Callable, Iterator

SENTENCE_END = re.compile(r"(?<=[.!?…])\s+(?=[A-ZА-Я\"'“])|(?<=[.!?…])$")
# Abbreviations that should not end a spoken sentence.
_NOT_END = re.compile(r"\b(Mr|Mrs|Ms|Dr|St|vs|e\.g|i\.e)\.$", re.IGNORECASE)

STYLE_RULES = {
    "plain": (
        "Rephrase what was said in simple, clear English, and say what is going "
        "on in the scene if that is what was asked."
    ),
    "vocab": (
        "Rephrase what was said in simple English, then pick out the one or two "
        "hardest words or idioms and explain them briefly."
    ),
    "russian": (
        "Answer mainly in English, but when a word or idiom is genuinely hard, "
        "give the Russian equivalent right after it."
    ),
}


def build_system_prompt(level: str = "B2", style: str = "plain",
                        game: str | None = None) -> str:
    title = game or "a video game"
    return f"""You help a {level}-level English learner follow the spoken dialogue in {title} while they play.

You will be given the subtitle lines captured from the screen over the last few minutes, newest last, with how long ago each one appeared. You may also be given a screenshot of the current scene. The player then asks a question out loud, such as "what did he just say?", "what are they talking about?", or "what does that word mean?".

{STYLE_RULES.get(style, STYLE_RULES["plain"])}

Your answer is converted to speech and played into the player's headset while the game is running, so:
- Keep it to two or three short sentences. Never more than four.
- Write plain spoken prose. No markdown, no bullet points, no headings, no emoji, no stage directions.
- Do not start with filler like "Sure", "Of course", "Great question" or "In this scene". Begin with the answer itself.
- Do not read the subtitle back word for word unless the player asked you to repeat it. Say what it means.
- Names of characters and places are fine to use as they appear.
- The subtitles come from OCR, so they may contain small errors. Read through obvious typos without commenting on them.
- If the question is about something that is not in the transcript at all, say so in one short sentence and offer your best guess from the scene.
- Never mention subtitles, OCR, transcripts, screenshots or that you are an assistant. Just answer as if you were watching the game with them."""


def split_sentences(text: str) -> tuple[list[str], str]:
    """Split off complete sentences, returning (sentences, remainder)."""
    parts: list[str] = []
    remainder = text
    while True:
        match = None
        for m in SENTENCE_END.finditer(remainder):
            if m.start() == 0:
                continue
            candidate = remainder[: m.start() + 1]
            if _NOT_END.search(candidate.strip()):
                continue
            match = m
            break
        if match is None:
            break
        end = match.end() if match.end() > match.start() else match.start() + 1
        chunk = remainder[:end].strip()
        if chunk:
            parts.append(chunk)
        remainder = remainder[end:]
        if not remainder.strip():
            remainder = ""
            break
    return parts, remainder


class Asker:
    """Thin wrapper over the Anthropic client with a self-healing model id."""

    def __init__(self, config, api_key: str | None = None):
        from anthropic import Anthropic

        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError(
                "No API key. Set the ANTHROPIC_API_KEY environment variable, or "
                "put it in a .env file next to config.yaml."
            )
        self.client = Anthropic(api_key=key)
        self.cfg = config
        self.model = str(config.model["name"])
        self.max_tokens = int(config.model["max_tokens"])
        self.notes: list[str] = []
        self.game: str | None = config.help.get("game") or None
        # Called with (input_tokens, output_tokens) after every answer.
        self.on_usage: Callable[[int, int], None] | None = None

    def resolve_model(self) -> str:
        """Fall back to an available model if the configured id is not valid."""
        try:
            available = [m.id for m in self.client.models.list(limit=50).data]
        except Exception as exc:
            self.notes.append(f"could not list models ({type(exc).__name__}); "
                              f"using {self.model} as configured")
            return self.model

        if self.model in available:
            return self.model

        # Prefer an exact family match, then any Sonnet, then anything at all.
        def pick(predicate) -> str | None:
            return next((m for m in available if predicate(m)), None)

        family = self.model.split("-")[1] if "-" in self.model else ""
        choice = (
            pick(lambda m: m.startswith(self.model))
            or (pick(lambda m: family and family in m) if family else None)
            or pick(lambda m: "sonnet" in m)
            or (available[0] if available else None)
        )
        if choice:
            self.notes.append(
                f"model '{self.model}' is not available to this key; using '{choice}'"
            )
            self.model = choice
        return self.model

    # -- the actual request ---------------------------------------------------

    def _build_content(self, transcript: str, question: str,
                       scene_jpeg: bytes | None) -> list[dict]:
        content: list[dict] = []
        if scene_jpeg:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.b64encode(scene_jpeg).decode("ascii"),
                    },
                }
            )
        body = "Dialogue from the last few minutes:\n"
        body += f"<transcript>\n{transcript or '(nothing captured yet)'}\n</transcript>\n\n"
        body += f"The player asks: {question}"
        content.append({"type": "text", "text": body})
        return content

    def stream(self, transcript: str, question: str,
               scene_jpeg: bytes | None = None) -> Iterator[str]:
        """Yield text deltas as they arrive."""
        system = build_system_prompt(
            level=str(self.cfg.help["level"]),
            style=str(self.cfg.help["style"]),
            game=self.game,
        )
        with self.client.messages.stream(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[
                {
                    "role": "user",
                    "content": self._build_content(transcript, question, scene_jpeg),
                }
            ],
        ) as stream:
            for delta in stream.text_stream:
                yield delta
            try:
                usage = stream.get_final_message().usage
                if self.on_usage:
                    self.on_usage(int(usage.input_tokens), int(usage.output_tokens))
            except Exception:
                pass  # usage is nice to have, never worth failing an answer over

    def stream_sentences(self, transcript: str, question: str,
                         scene_jpeg: bytes | None = None,
                         on_delta: Callable[[str], None] | None = None
                         ) -> Iterator[str]:
        """Yield complete sentences, so speech can start before the answer ends."""
        pending = ""
        for delta in self.stream(transcript, question, scene_jpeg):
            if on_delta:
                on_delta(delta)
            pending += delta
            sentences, pending = split_sentences(pending)
            for sentence in sentences:
                yield sentence
        tail = pending.strip()
        if tail:
            yield tail
