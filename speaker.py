"""朗读：封装 AVSpeechSynthesizer，可打断、按语言选音、可调语速。"""

import re

from AVFoundation import (
    AVSpeechSynthesizer,
    AVSpeechUtterance,
    AVSpeechSynthesisVoice,
    AVSpeechBoundaryImmediate,
)

# 语速档位：映射到 AVSpeechUtterance 的 rate（0.0~1.0，默认约 0.5）
RATES = {
    "slow": 0.42,
    "normal": 0.5,
    "fast": 0.58,
}

_CJK = re.compile(r"[一-鿿㐀-䶿]")


def _language_for(text: str) -> str:
    """含中日韩汉字则用中文语音，否则英文。"""
    return "zh-CN" if _CJK.search(text) else "en-US"


class Speaker:
    def __init__(self, rate: str = "normal"):
        self._synth = AVSpeechSynthesizer.alloc().init()
        self._rate_key = rate if rate in RATES else "normal"

    def set_rate(self, rate: str) -> None:
        if rate in RATES:
            self._rate_key = rate

    def speak(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        # 打断上一句，避免多段选择叠读。
        self.stop()

        utterance = AVSpeechUtterance.speechUtteranceWithString_(text)
        voice = AVSpeechSynthesisVoice.voiceWithLanguage_(_language_for(text))
        if voice is not None:
            utterance.setVoice_(voice)
        utterance.setRate_(RATES[self._rate_key])
        self._synth.speakUtterance_(utterance)

    def stop(self) -> None:
        if self._synth.isSpeaking():
            self._synth.stopSpeakingAtBoundary_(AVSpeechBoundaryImmediate)
