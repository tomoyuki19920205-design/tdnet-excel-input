from dotenv import load_dotenv
load_dotenv()

from pprint import pprint
from lib.backfill.segment_ai_fallback import extract_segments_with_ai

pdf_path = r"data\セグメントサンプル20件\1515日鉄鉱業.pdf"
ticker = "1515"
title = "1515 test"

result = extract_segments_with_ai(
    pdf_path=pdf_path,
    ticker=ticker,
    title=title,
    model="gpt-4o-mini",
)

print("=== RESULT TYPE ===")
print(type(result).__name__)
print("=== RESULT ===")
pprint(result)
