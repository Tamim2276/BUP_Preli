import copy
import logging
import time

from . import llm
from .fallback import parse_note
from .guardrails import GuardrailError, validate_entry

log = logging.getLogger("gridwise.interpreter")

BUDGET_S = 24.0
MAX_CACHE = 1000
_cache: dict = {}
_now = time.monotonic


def _check_all(items: list, n_notes: int, capacity: float) -> tuple[dict, list[str]]:
    good, errors = {}, []
    for pos, item in enumerate(items):
        if not isinstance(item, dict):
            errors.append(f"entry {pos} is not an object")
            continue
        idx = item.get("note_index", pos)
        if isinstance(idx, bool) or not isinstance(idx, int) or not 0 <= idx < n_notes:
            errors.append(f"entry {pos} has invalid note_index {idx!r}")
            continue
        if idx in good:
            errors.append(f"duplicate note_index {idx}")
            continue
        try:
            good[idx] = validate_entry(item, capacity)
        except GuardrailError as e:
            errors.append(f"note {idx}: {e}")
    missing = [i for i in range(n_notes) if i not in good]
    if missing:
        errors.append(f"missing valid entries for note_index {missing}")
    return good, errors


def _ask_llm(notes: list[str], battery: dict, capacity: float, start: float) -> dict:
    good: dict = {}
    for provider in llm.PROVIDERS:
        feedback = ""
        for attempt in (1, 2):
            if _now() - start + llm.TIMEOUT_S > BUDGET_S:
                log.warning("LLM time budget used up before %s", provider["name"])
                return good
            try:
                items = llm.call(provider, notes, battery, feedback)
            except ValueError:
                log.warning("%s returned unusable output (attempt %d)", provider["name"], attempt)
                feedback = "the output was not valid JSON in the required format"
                continue
            except Exception as e:
                log.warning("%s unavailable: %s", provider["name"], type(e).__name__)
                break
            fresh, errors = _check_all(items, len(notes), capacity)
            for idx, entry in fresh.items():
                good.setdefault(idx, entry)
            if len(good) == len(notes):
                log.info("notes interpreted by %s", provider["name"])
                return good
            feedback = "; ".join(errors)
            log.warning("%s output rejected by guardrails (attempt %d): %s",
                        provider["name"], attempt, feedback)
    return good


def interpret(notes: list[str], battery: dict) -> list[dict]:
    capacity = float(battery["capacity_kwh"])
    key = (tuple(notes), capacity, float(battery["minimum_energy_kwh"]))
    if key in _cache:
        return copy.deepcopy(_cache[key])
    good = _ask_llm(notes, battery, capacity, _now())
    from_llm = len(good) == len(notes)
    for i, note in enumerate(notes):
        if i not in good:
            log.warning("note %d: using rule-based fallback parser", i)
            try:
                good[i] = validate_entry(parse_note(note, capacity), capacity)
            except Exception:
                good[i] = validate_entry({"directive_type": "no_op"}, capacity)
    result = [{"note_index": i, **good[i]} for i in range(len(notes))]
    if from_llm and len(_cache) < MAX_CACHE:
        _cache[key] = copy.deepcopy(result)
    return result
