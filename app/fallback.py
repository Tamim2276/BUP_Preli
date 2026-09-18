import re

WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
         "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
FRACTIONS = {"one-fifth": 0.2, "a fifth": 0.2, "one fifth": 0.2, "half": 0.5,
             "one-quarter": 0.25, "a quarter": 0.25, "one-fourth": 0.25, "one quarter": 0.25,
             "three-quarters": 0.75, "three quarters": 0.75, "one-third": 1 / 3,
             "a third": 1 / 3, "two-thirds": 2 / 3, "one-tenth": 0.1, "a tenth": 0.1}
TIME = (r"(noon|midday|midnight|\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?|"
        r"(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)(?:\s*[ap]\.?m\.?)?)")
WINDOW = re.compile(rf"(?:from|between)?\s*{TIME}\s*(?:until|till|to|through|and|-|–)\s*{TIME}", re.I)


def _parse_time(tok: str, is_end: bool):
    t = tok.strip().lower().replace(".", "")
    if t in ("noon", "midday"):
        return 12, "fixed"
    if t == "midnight":
        return (24 if is_end else 0), "fixed"
    mer = "pm" if t.endswith("pm") else "am" if t.endswith("am") else None
    t = re.sub(r"\s*[ap]m$", "", t)
    word = t in WORDS
    h = WORDS[t] if word else int(t.split(":")[0])
    if ":" in t and mer is None:
        return h, "fixed"
    return h, mer or ("word" if word else "bare")


def _to24(h, mer):
    if mer == "pm":
        return h % 12 + 12
    if mer == "am":
        return h % 12
    return h


def find_hours(text: str):
    m = WINDOW.search(text)
    if not m:
        return None
    (h1, m1), (h2, m2) = _parse_time(m.group(1), False), _parse_time(m.group(2), True)
    if m1 in ("bare", "word") and m2 in ("am", "pm"):
        m1 = m2 if h1 % 12 <= h2 % 12 else ("am" if m2 == "pm" else "pm")
    if m2 in ("bare", "word") and m1 in ("am", "pm"):
        m2 = m1 if h2 % 12 >= h1 % 12 else ("pm" if m1 == "am" else "am")
    if m1 in ("bare", "word") and m2 in ("bare", "word") and h1 <= 12 and h2 <= 12 and h2 <= 7:
        m2 = "pm"
        m1 = "pm" if h1 <= h2 else "am"
    s = _to24(h1, None if m1 == "fixed" else m1)
    e = _to24(h2, None if m2 == "fixed" else m2)
    if s == e:
        return None
    hrs = list(range(s, e)) if e > s else list(range(s, 24)) + list(range(0, e))
    return sorted(h for h in hrs if 0 <= h <= 23)


def _percent(text):
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent)", text, re.I)
    return float(m.group(1)) if m else None


def _kwh(text):
    m = re.search(r"(\d+(?:\.\d+)?)\s*kwh", text, re.I)
    return float(m.group(1)) if m else None


def _entry(t, adj, why):
    return {"directive_type": t, "structured_adjustment": adj, "explanation": why}


def parse_note(note: str, capacity: float) -> dict:
    low = note.lower()
    no_op = _entry("no_op", None, "This note does not affect today's 24-hour energy schedule.")
    if re.search(r"next (week|month|year)|last (week|month)|yesterday", low):
        return no_op
    hours = find_hours(note)
    if hours is None:
        return no_op
    solar = re.search(r"solar|\bpv\b|photovoltaic|panel", low)
    battery = re.search(r"batter|charg|reserve|stored|state of charge", low)
    grid = re.search(r"\bgrid\b|import|intake|feeder|transformer|substation|utility supply", low)
    negate = re.search(r"\bnot\b|\bno\b|n't|disabled|unavailable|isolated|offline|blocked|"
                       r"prohibited|out of service|locked", low)
    if solar and not battery:
        pct = _percent(note)
        factor = None
        if pct is not None:
            reduce_by = (re.search(r"reduc|cut|drop by|lower by|decrease|less", low)
                         and not re.search(r"(drop|fall|reduc\w*|decrease\w*)\s+to", low))
            factor = 1 - pct / 100 if reduce_by else pct / 100
        else:
            for k, v in FRACTIONS.items():
                if k in low:
                    factor = v
                    break
            if factor is None and re.search(r"no solar|fully unavailable|completely|zero", low):
                factor = 0.0
        if factor is not None:
            return _entry("solar_reduction", {"hours": hours, "factor": round(factor, 4)},
                          "Usable solar is reduced during the stated window.")
        return no_op
    if grid:
        v = _kwh(note)
        if v is not None:
            return _entry("max_grid_window", {"hours": hours, "max_grid_kwh": v},
                          "Grid import is capped during the stated window.")
    if battery and re.search(r"at least|reserve|minimum|remain|keep|stored|below", low):
        v = _kwh(note)
        pct = _percent(note)
        if v is None and pct is not None:
            v = capacity * pct / 100
        if v is None and "half" in low:
            v = capacity / 2
        if v is None and re.search(r"fully charged|full charge", low):
            v = capacity
        if v is not None:
            return _entry("minimum_battery_reserve", {"hours": hours, "minimum_energy_kwh": v},
                          "Battery must keep the stated reserve during the window.")
    if negate and re.search(r"discharg", low):
        return _entry("no_discharge_window", {"hours": hours},
                      "Battery discharge is blocked during the stated window.")
    if negate and re.search(r"\bcharg", low):
        return _entry("no_charge_window", {"hours": hours},
                      "Battery charging is blocked during the stated window.")
    return no_op
