"""Scrape today's Thrice trivia game from https://thrice.geekswhodrink.com/.

The game shows 5 questions, each with 3 clues that share a single answer.
We drive the page with Playwright: read each clue, skip to the next clue,
then "give up" to reveal the answer, then advance to the next question.
"""
import asyncio
import re
from dataclasses import dataclass, field, asdict
from typing import Optional

from playwright.async_api import Page, async_playwright

URL = "https://thrice.geekswhodrink.com/"


@dataclass
class Question:
    number: int
    category: str
    clues: list[str] = field(default_factory=list)
    answer: Optional[str] = None


def squeeze(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


async def dismiss_modal(page: Page) -> None:
    # The "How to Play" modal intercepts pointer events on first visit.
    # Try clicking Close, then fall back to ripping the modal out of the DOM.
    for sel in ['button:has-text("Close")', 'button[aria-label="Close"]']:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=500):
                await btn.click()
                await page.wait_for_timeout(200)
        except Exception:
            pass
    await page.evaluate(
        """() => {
            document.querySelectorAll('[data-controller~="first-time-how-to-play"], [data-controller~="modal"], #how_to_modal')
                .forEach(el => el.remove());
        }"""
    )


async def read_main(page: Page) -> dict:
    """Extract the visible question state from the turbo-frame#main region."""
    frame = page.locator("turbo-frame#main")

    async def text_of(sel: str) -> str:
        loc = frame.locator(sel).first
        try:
            return squeeze(await loc.inner_text(timeout=500))
        except Exception:
            return ""

    category = (await text_of(".orange-bar")).replace("Category:", "").replace("CATEGORY:", "").strip()
    question_heading = await text_of(".question-heading")
    current_clue = await text_of(".clue-text")

    m = re.search(r"Question\s+(\d+)", question_heading, re.I)
    qnum = int(m.group(1)) if m else 0

    # The "answer is X" appears when the question ends.
    full_text = squeeze(await frame.inner_text())
    answer = None
    m = re.search(r"The answer is\s+(.+?)(?:\s+NEXT QUESTION|\s+That'?s a wrap|$)", full_text, re.I)
    if m:
        answer = m.group(1).strip()

    return {
        "category": category,
        "qnum": qnum,
        "clue": current_clue,
        "answer": answer,
        "full_text": full_text,
    }


class ScrapeError(RuntimeError):
    pass


async def _clue_text(page: Page) -> str:
    try:
        return squeeze(await page.locator("turbo-frame#main .clue-text").first.inner_text(timeout=500))
    except Exception:
        return ""


async def click_advance(page: Page) -> bool:
    """Click whichever advance button is currently shown.

    The wait condition depends on the button:
      - skip-preloader (more clues left, or give-up): wait for clue text to change
        OR for the "The answer is" reveal to appear.
      - Next Question: wait for the "answer is" reveal to be replaced by a new clue.
    """
    skip_sel = 'turbo-frame#main button[data-action*="skip-preloader#skipQuestion"]'
    next_sels = [
        'turbo-frame#main a:has-text("Next Question")',
        'turbo-frame#main button:has-text("Next Question")',
    ]

    # Try skip-preloader first.
    btn = page.locator(skip_sel).first
    try:
        if await btn.is_visible(timeout=300):
            before_clue = await _clue_text(page)
            try:
                await btn.click(timeout=3000)
            except Exception:
                return False
            await page.wait_for_function(
                """(prev) => {
                    const f = document.querySelector('turbo-frame#main');
                    if (!f) return false;
                    if (/The answer is/i.test(f.innerText || "")) return true;
                    const cur = (f.querySelector('.clue-text')?.innerText || "")
                        .trim().replace(/\\s+/g, " ");
                    return cur && cur !== prev;
                }""",
                arg=before_clue,
                timeout=8000,
            )
            return True
    except Exception:
        pass

    # Then try the next-question control.
    for sel in next_sels:
        btn = page.locator(sel).first
        try:
            if not await btn.is_visible(timeout=300):
                continue
        except Exception:
            continue
        try:
            await btn.click(timeout=3000)
        except Exception:
            continue
        # Wait for the answer-reveal to clear AND a fresh clue to appear,
        # OR for the end-of-game wrap screen.
        await page.wait_for_function(
            """() => {
                const f = document.querySelector('turbo-frame#main');
                if (!f) return false;
                const t = f.innerText || "";
                if (/that'?s a wrap/i.test(t)) return true;
                if (/The answer is/i.test(t)) return false;
                const cur = (f.querySelector('.clue-text')?.innerText || "").trim();
                return cur.length > 0;
            }""",
            timeout=8000,
        )
        return True
    return False


async def scrape() -> dict:
    questions: dict[int, Question] = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context()
        # Block analytics/ad junk to speed things up.
        await ctx.route(
            re.compile(r"(google-analytics|googletagmanager|doubleclick|facebook|hotjar|clarity|adroll|hubapi|hsadspixel|tiktok|linkedin|bing\.com|adservices)"),
            lambda route: route.abort(),
        )
        page = await ctx.new_page()
        await page.goto(URL, wait_until="domcontentloaded")
        await page.wait_for_selector("turbo-frame#main", timeout=10000)
        await dismiss_modal(page)
        # Make sure the first clue is actually rendered before we start reading.
        await page.wait_for_function(
            "() => (document.querySelector('turbo-frame#main .clue-text')?.innerText || '').trim().length > 0",
            timeout=10000,
        )

        for step in range(40):  # generous upper bound; 5 questions × 4 clicks = 20
            state = await read_main(page)
            qnum = state["qnum"]

            if qnum and qnum not in questions:
                questions[qnum] = Question(number=qnum, category=state["category"])

            if qnum and state["clue"] and not state["answer"] and len(questions[qnum].clues) < 3:
                q = questions[qnum]
                if state["clue"] not in q.clues:
                    q.clues.append(state["clue"])
                if not q.category and state["category"]:
                    q.category = state["category"]

            # If the answer is revealed for this question, capture it.
            if qnum and state["answer"]:
                questions[qnum].answer = state["answer"]

            # End-of-game signal.
            if "That's a wrap" in state["full_text"] or "that's a wrap" in state["full_text"].lower():
                # Make sure final answer is captured before bailing.
                if qnum and state["answer"]:
                    questions[qnum].answer = state["answer"]
                break

            if not await click_advance(page):
                break

        await browser.close()

    ordered = [questions[k] for k in sorted(questions)]

    # Validation — every question must have exactly 3 clues and an answer.
    expected_questions = 5
    problems: list[str] = []
    if len(ordered) != expected_questions:
        problems.append(f"got {len(ordered)} questions, expected {expected_questions}")
    for q in ordered:
        if len(q.clues) != 3:
            problems.append(f"Q{q.number} ({q.category!r}) has {len(q.clues)} clue(s), expected 3: {q.clues}")
        if not q.answer:
            problems.append(f"Q{q.number} ({q.category!r}) has no answer")
    if problems:
        raise ScrapeError("Incomplete scrape:\n  - " + "\n  - ".join(problems))

    return {"url": URL, "questions": [asdict(q) for q in ordered]}


if __name__ == "__main__":
    import json
    result = asyncio.run(scrape())
    print(json.dumps(result, indent=2, ensure_ascii=False))
