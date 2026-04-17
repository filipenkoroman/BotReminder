from .config import TEXT_MODEL_PRICING, TRANSCRIBE_PRICING_PER_MINUTE


def rough_token_count(text: str) -> int:
    return max(1, int(len(text) / 4))

def text_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    pricing = TEXT_MODEL_PRICING.get(model, TEXT_MODEL_PRICING["gpt-5.4-nano"])
    return (input_tokens / 1_000_000) * pricing["input"] + (output_tokens / 1_000_000) * pricing["output"]

def transcription_cost_usd(model: str, seconds: float) -> float:
    per_minute = TRANSCRIBE_PRICING_PER_MINUTE.get(model, TRANSCRIBE_PRICING_PER_MINUTE["gpt-4o-mini-transcribe"])
    return (seconds / 60) * per_minute
