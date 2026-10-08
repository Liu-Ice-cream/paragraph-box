"""Shared Unicode matching and original-text offsets for preview highlighting."""

import unicodedata


def normalize(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def match_ranges(text: str, tokens: list[str]) -> list[list[int]]:
    # Offsets count Unicode code points, not UTF-16 units. The UI uses Array.from.
    parts, positions = [], []
    clusters = []
    for index, char in enumerate(text):
        if clusters and unicodedata.combining(char):
            clusters[-1][1] = index + 1
        else:
            clusters.append([index, index + 1])
    ends = []
    for start, end in clusters:
        part = unicodedata.normalize("NFKC", text[start:end]).casefold()
        parts.append(part)
        positions.extend([start] * len(part))
        ends.extend([end] * len(part))
    folded = "".join(parts)
    spans = []
    for token in tokens:
        if not token:
            continue
        start = folded.find(token)
        while start >= 0:
            spans.append([positions[start], ends[start + len(token) - 1]])
            start = folded.find(token, start + 1)
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def match_details(text: str, heading: str, snippet: str, query: str) -> dict:
    tokens = list(dict.fromkeys(normalize(query).split()))
    body = normalize(text)
    kind = "body" if all(token in body for token in tokens) else "heading" if not any(token in body for token in tokens) else "mixed"
    return {"match_kind": kind, "text_matches": match_ranges(text, tokens),
            "snippet_matches": match_ranges(snippet, tokens), "heading_matches": match_ranges(heading, tokens)}
