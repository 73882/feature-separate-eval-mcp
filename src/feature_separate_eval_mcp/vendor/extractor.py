"""Extract Chinese or English claims from claim-section text.

Originally vendored from claim-decomposition-no-gt-eval, then reduced to the part
this skill actually runs: split_claims. Claim text comes from the CLMS field of
the same upstream service the decomposition skill calls, so the original PDF /
DOCX / JSON file loaders (load_file, load_cases and their helpers) were removed
along with the PatentCase model they returned. Restore them from the source
project if local-file input is ever needed here.
"""

import re


CLAIM_NUMBER = re.compile(
    r"(?m)^\s*(?P<number>[1-9]\d{0,2})\s*[\.．、\)]\s*(?=\S)"
)
CN_SECTION_START = re.compile(r"(?im)^\s*权\s*利\s*要\s*求\s*书\s*$")
EN_SECTION_START = re.compile(
    r"(?im)^\s*(?:"
    r"(?:what\s+is\s+claimed\s+is|what\s+is\s+claimed)\s*:?"
    r"|(?:we|i)\s+claim\s*:?"
    r"|(?:the\s+)?(?:invention\s+)?claims?\s+(?:is|are)\s*:?"
    r"|claims?\s*:?"
    r")\s*$"
)
SECTION_END = re.compile(
    r"(?im)^\s*(?:说\s*明\s*书|摘\s*要|description|detailed\s+description|abstract)\s*$"
)
CN_CLAIMS_PAGE_FOOTER = re.compile(
    r"权\s*利\s*要\s*求\s*书\s*(?P<page>\d+)\s*/\s*(?P<total>\d+)\s*页"
)
CN_CLAIMS_RUNNING_FOOTER_BLOCK = re.compile(
    r"权\s*利\s*要\s*求\s*书\s*\d+\s*/\s*\d+\s*页"
    r"\s*\n\s*\d+\s*\n\s*CN\s*[\d\s]+[A-Z]\s*\n\s*\d+\s*",
    re.I,
)




class InputError(ValueError):
    """Raised when an input cannot be converted to patent claims."""










def _claims_section(text: str) -> str:
    starts = []
    for pattern in (CN_SECTION_START, EN_SECTION_START):
        match = pattern.search(text)
        if match:
            starts.append(match.end())
    start = min(starts) if starts else 0
    # CN patent PDFs commonly omit a standalone section heading in extracted
    # text but retain page footers such as “权 利 要 求 书 3/3 页”.  The footer
    # whose page equals total is a stronger boundary than later numbered
    # description paragraphs, which otherwise look like claim headings.
    # An explicit “说明书”/“description” heading is the authoritative boundary.
    # The final-page footer is only a fallback: it marks the bottom of the last
    # claim page, and claims continue past it whenever the page break falls
    # mid-list, so trusting it first truncates the claim list.
    end_match = SECTION_END.search(text, start)
    if end_match:
        return text[start:end_match.start()].strip()
    completed_footers = [
        match.start() for match in CN_CLAIMS_PAGE_FOOTER.finditer(text, start)
        if match.group("page") == match.group("total")
    ]
    end = min(completed_footers) if completed_footers else len(text)
    return text[start:end].strip()


def _claim_heading_matches(section: str):
    """Keep only headings that form a real claim sequence.

    Extracted patent text mixes genuine claim headings with noise that looks
    identical to the regex: in-claim sub-step markers such as “1)”, figure
    reference numerals that happen to land at a line start (“…a field\n104.”),
    and OCR debris from PCT search-report tables. Claim numbering, however, is
    always strictly increasing and starts at 1, so the longest strictly
    increasing run that starts at 1 is the claim list; everything outside it is
    noise. Ties keep the earliest run, which is where the claim section starts.
    """
    matches = list(CLAIM_NUMBER.finditer(section))
    if not matches:
        return []
    numbers = [int(match.group("number")) for match in matches]
    # best[index] = length of the longest increasing run ending at index
    best = [1] * len(matches)
    previous = [-1] * len(matches)
    for index in range(len(matches)):
        for earlier in range(index):
            if (
                numbers[earlier] < numbers[index]
                and best[earlier] + 1 > best[index]
            ):
                best[index] = best[earlier] + 1
                previous[index] = earlier
    candidates = [
        index for index in range(len(matches))
        if _run_start_number(numbers, previous, index) == 1
    ]
    if not candidates:
        return []
    end = max(candidates, key=lambda index: (best[index], -index))
    run = []
    cursor = end
    while cursor != -1:
        run.append(cursor)
        cursor = previous[cursor]
    return [matches[index] for index in reversed(run)]


def _claims_section_without_heading(text: str):
    """Locate the claim list in a document that has no claim heading.

    Claims are the last numbered run in a publication, so the search starts at
    the final “1.” that is followed by a longer increasing run than any noise
    before it. Returning an empty string means no usable run exists, which the
    caller reports as an extraction failure rather than guessing.
    """
    best_section = ""
    best_length = 0
    for match in CLAIM_NUMBER.finditer(text):
        if int(match.group("number")) != 1:
            continue
        candidate = text[match.start():].strip()
        found = _claim_heading_matches(candidate)
        if len(found) > best_length:
            best_length = len(found)
            best_section = candidate
    return best_section if best_length >= 2 else ""


def _run_start_number(numbers, previous, index):
    cursor = index
    while previous[cursor] != -1:
        cursor = previous[cursor]
    return numbers[cursor]


def split_claims(text: str):
    """Split a claim section while preserving the original claim numbers."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    section = _claims_section(normalized)
    section = CN_CLAIMS_RUNNING_FOOTER_BLOCK.sub("\n", section)
    if not section:
        raise InputError("no claim text found")
    matches = _claim_heading_matches(section)
    if not matches:
        # Some US publications carry no claim heading at all: the description
        # runs straight into “1. A method …”.  Heading-based slicing then lands
        # on the description, so fall back to locating the claim run in the
        # whole document.
        fallback = _claims_section_without_heading(normalized)
        if fallback:
            section = fallback
            matches = _claim_heading_matches(section)
    if not matches:
        # No heading at all is a legitimate single-claim text. Headings that all
        # failed validation mean the section is figure pages, description body
        # or OCR debris — passing that on would bill a Judge call for garbage.
        if CLAIM_NUMBER.search(section):
            raise InputError(
                "numbered lines were found but none form a claim sequence "
                "starting at 1; the extracted text is probably not the claim "
                "section"
            )
        return {"claim_1": section.strip()}

    claims = {}
    for index, match in enumerate(matches):
        number = int(match.group("number"))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(section)
        body = section[match.end():end].strip()
        if not body:
            continue
        key = f"claim_{number}"
        if key in claims:
            raise InputError(
                f"duplicate claim number after heading validation: {number}"
            )
        claims[key] = body
    if not claims:
        raise InputError("numbered claim headings were found but no claim body was extracted")
    return claims










