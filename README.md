# thrice-counter

Scrapes today's trivia from https://thrice.geekswhodrink.com/ and shows it in a small web UI.

## How it works

The site renders questions through Turbo/Stimulus, so we drive it with Playwright instead of plain HTTP. Each game = 5 questions, 3 clues each, sharing one answer. The scraper:

1. Opens the page and rips out the "How to Play" modal.
2. Reads the visible clue + category + question number from `turbo-frame#main`.
3. Clicks "show me the next clue" / "Yeah, I give up" / "Next Question" to advance.
4. Captures "The answer is X" when each question ends.
5. Stops on "That's a wrap!".

## Run

```
pip install playwright flask
playwright install chromium
python app.py
# open http://127.0.0.1:5000/
```

## Files

- [scraper.py](scraper.py) — Playwright scraper. Run standalone to print JSON.
- [app.py](app.py) — Flask app with `/api/scrape` (30 min cache) and `/api/refresh`.
- [templates/index.html](templates/index.html) — single-page frontend.
