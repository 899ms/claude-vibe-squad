"""POST /summarize — Gemini Flash via the agy CLI (see daemon/flash_summarizer.py)."""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from daemon.flash_summarizer import FlashSummarizer, SummarizerError

router = APIRouter()


class SummarizeRequest(BaseModel):
    text: str
    instructions: str | None = None


@router.post("/summarize")
async def summarize(req: SummarizeRequest):
    """Summarize text using Gemini Flash through agy."""
    try:
        summarizer = FlashSummarizer()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail="summarization unavailable: agy CLI not found",
        ) from exc
    try:
        summary = await summarizer.summarize(req.text, req.instructions)
    except SummarizerError as exc:
        raise HTTPException(status_code=502, detail=f"summarization failed: {exc}") from exc
    return {"summary": summary}
