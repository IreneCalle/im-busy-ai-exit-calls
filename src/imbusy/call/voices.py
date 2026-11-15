"""Catalog voices for ConversationRelay. Stock voices only: no cloning, by design.

Voice strings follow Twilio's TTS voice table (Google Chirp3-HD and Amazon Polly Neural voices are
available for es-ES and en-US). If a provider/voice pair is invalid, Twilio ends the session with
error 64101, so every entry here must be checked on a real call before it ships.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from imbusy.config import TtsProvider
from imbusy.excuse.schema import Language, VoiceGender


@dataclass(frozen=True)
class VoiceSpec:
    id: str  # stable key stored in CallScript.voice_id
    language: Language
    gender: VoiceGender
    provider: TtsProvider
    voice: str  # value of the `voice` attribute in <ConversationRelay>
    label: str


CATALOG: tuple[VoiceSpec, ...] = (
    # Google Chirp3-HD (generative). Default provider.
    VoiceSpec("es-f-aoede", Language.ES_ES, VoiceGender.FEMALE, TtsProvider.GOOGLE,
              "es-ES-Chirp3-HD-Aoede", "Spanish, female, warm"),
    VoiceSpec("es-f-kore", Language.ES_ES, VoiceGender.FEMALE, TtsProvider.GOOGLE,
              "es-ES-Chirp3-HD-Kore", "Spanish, female, brisk"),
    VoiceSpec("es-m-charon", Language.ES_ES, VoiceGender.MALE, TtsProvider.GOOGLE,
              "es-ES-Chirp3-HD-Charon", "Spanish, male"),
    VoiceSpec("en-f-aoede", Language.EN_US, VoiceGender.FEMALE, TtsProvider.GOOGLE,
              "en-US-Chirp3-HD-Aoede", "English, female, warm"),
    VoiceSpec("en-f-kore", Language.EN_US, VoiceGender.FEMALE, TtsProvider.GOOGLE,
              "en-US-Chirp3-HD-Kore", "English, female, brisk"),
    VoiceSpec("en-m-charon", Language.EN_US, VoiceGender.MALE, TtsProvider.GOOGLE,
              "en-US-Chirp3-HD-Charon", "English, male"),
    # Amazon Polly Neural.
    VoiceSpec("es-f-lucia", Language.ES_ES, VoiceGender.FEMALE, TtsProvider.AMAZON,
              "Lucia-Neural", "Spanish, female"),
    VoiceSpec("es-m-sergio", Language.ES_ES, VoiceGender.MALE, TtsProvider.AMAZON,
              "Sergio-Neural", "Spanish, male"),
    VoiceSpec("en-f-joanna", Language.EN_US, VoiceGender.FEMALE, TtsProvider.AMAZON,
              "Joanna-Neural", "English, female"),
    VoiceSpec("en-m-matthew", Language.EN_US, VoiceGender.MALE, TtsProvider.AMAZON,
              "Matthew-Neural", "English, male"),
    # ElevenLabs: Twilio's documented per-language defaults. Gender not documented; treated as female
    # until confirmed by ear.
    VoiceSpec("es-x-elevenlabs", Language.ES_ES, VoiceGender.FEMALE, TtsProvider.ELEVENLABS,
              "6xftrpatV0jGmFHxDjUv", "Spanish, ElevenLabs default"),
    VoiceSpec("en-x-elevenlabs", Language.EN_US, VoiceGender.FEMALE, TtsProvider.ELEVENLABS,
              "UgBBYS2sOqTuMpoF3BR0", "English, ElevenLabs default"),
)  # fmt: skip


class VoiceCatalog:
    """Lookup over CATALOG restricted to the configured TTS provider."""

    def __init__(self, provider: TtsProvider, voices: Iterable[VoiceSpec] = CATALOG) -> None:
        self.provider = provider
        self._voices = [v for v in voices if v.provider == provider]
        self._by_id = {v.id: v for v in voices}

    def get(self, voice_id: str) -> VoiceSpec:
        """Return a voice by id; raises `KeyError` for unknown ids."""
        return self._by_id[voice_id]

    def pick(self, language: Language, gender: VoiceGender) -> VoiceSpec:
        """First voice for the language and gender; falls back to any voice in the language."""
        for voice in self._voices:
            if voice.language == language and voice.gender == gender:
                return voice
        for voice in self._voices:
            if voice.language == language:
                return voice
        raise LookupError(f"no {self.provider} voice for {language}")
