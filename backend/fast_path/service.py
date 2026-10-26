# Copyright (c) 2026 Yash Garad. All rights reserved.

"""Answer the common question families without a language model.

    try_answer(question) -> FastAnswer | None

None means "not handled here", and the caller runs the normal pipeline. That is
the default for anything this module is not certain about, and being certain is
deliberately hard: the question must match one family's pattern, every value it
names must already exist in the database's own vocabulary, and no other family
may match it as well.

WHY "NONE" RATHER THAN A BEST GUESS
A fast path that answers 95% correctly is worse than one that answers 60% of
questions and declines the rest, because the 5% arrive as confident wrong
figures with no model and no verifier between them and the reader. Declining
costs a slow answer. Guessing costs a wrong one.

WHAT IS PRESERVED FROM THE SLOW PATH
  * a failed lookup is reported as a failure, never as "no such record" — the
    founding rule of this system. A database error here returns None so the
    normal pipeline runs and produces its own degradation message; it never
    renders "there are 0".
  * a genuine zero says so in words chosen for a zero, and is never dressed up
    as an absence of data.
  * the audit log still records the question, the SQL and the answer, because
    the caller logs it exactly as it logs a model-generated one.
"""

import logging
import re
import time

from common import schema_map

from . import entities
from .intents import _COUNT, _FACULTY, AVERAGE_METRICS, INTENTS, _AVERAGE_RE

# "how many [up to three words] faculty|professors|lecturers" - the shape of a
# count over the faculty records. The words between are filters ("Assistant
# Professor", "Expert") or are declined by _unanswered_constraint ("female").
_GENERIC_COUNT_RE = re.compile(
    rf"{_COUNT}\s+(?:[\w-]+\s+){{0,3}}?(?:{_FACULTY}|professors?|lecturers?)\b", re.IGNORECASE
)

logger = logging.getLogger("fast_path")


class FastAnswer:
    __slots__ = ("text", "intent", "sql", "params", "rows", "elapsed_ms")

    def __init__(self, text, intent, sql, params, rows, elapsed_ms):
        self.text = text
        self.intent = intent
        self.sql = sql
        self.params = params
        self.rows = rows
        self.elapsed_ms = elapsed_ms


def _find_values(question, slots, vocab):
    """Values only - see _resolve. Kept because callers and tests use it."""
    resolved = _resolve(question, slots, vocab)
    return None if resolved is None else resolved[0]


def _resolve(question, slots, vocab):
    """Resolve each slot to a value the DATABASE actually contains.

    Returns (values, spans) - the spans are the character ranges the values
    were read from, so the caller can check what the question says BESIDES
    them (see _unanswered_constraint).

    Longest match first, so "Computer Science" is not resolved as "Science".
    That is a correctness property: both are real departments here, and the
    shorter one is a substring of the longer.

    Returns None if any slot cannot be filled, or if a slot is ambiguous.
    """
    lowered = question.lower()
    values = []
    consumed = []  # (start, end) spans already claimed by an earlier slot

    for logical_table, column in slots:
        candidates = vocab.get((logical_table, column))
        if not candidates:
            return None

        best = None
        for candidate in sorted(candidates, key=len, reverse=True):
            # Word-boundary match, so "Education" does not match inside
            # "Co-Education" and "Arts" does not match inside "Martial Arts".
            for m in re.finditer(rf"\b{re.escape(candidate.lower())}\b", lowered):
                span = (m.start(), m.end())
                if any(span[0] < e and s < span[1] for s, e in consumed):
                    continue  # already claimed by a longer value in another slot
                best = (candidate, span)
                break
            if best:
                break

        if best is None:
            return None
        values.append(best[0])
        consumed.append(best[1])

    return values, consumed


# Words a question may contain BESIDES the values a family consumed, and still
# mean exactly what that family computes. Anything else - a negation, an "or",
# a number, an attribute the family has no slot for ("female", "senior",
# "older") - is a constraint the fixed statement would silently ignore.
#
# This is an ALLOWLIST on purpose. The fast path's founding rule is that it
# declines whatever it is not certain about; a denylist of "dangerous words"
# would be certain about everything it had not thought of.
_FILLER = frozenset("""
    a an the of in at on to for from with within inside under by as and
    how many much number no nos count total totals overall altogether all entire whole
    what whats what's which who whose is are was were be there exist exists existing
    do does did have has having hold holds holding held currently current now
    rank ranks ranked designated designation position level levels grade
    department departments dept depts faculty faculties facultys staff teachers teacher
    lecturers academics academic members member people persons employees
    records record entries rows college colleges our listed recorded marked
    across belong belongs belonging working work works employed classified category
    come comes coming
    competency competencies university universities type types institution institutions
    assessed need needs development lms usage use uses using used frequency frequently
    give tell show me us please can could would you find get list s
    average mean avg score scores value index per year years hours week
    data dataset database survey
""".split())

_LEARNING_MANAGEMENT = re.compile(r"\blearning\s+management(?:\s+systems?)?\b")
_RISKY_SYMBOLS = re.compile(r"[<>=%!/|&]")


def _unanswered_constraint(question, spans, vocab, extra_spans=()):
    """The first thing the question asks that the matched family will NOT apply,
    or None when the family answers the whole question.

    WHY THIS EXISTS. A family is a fixed statement with fixed slots. Matching
    a pattern and filling every slot was taken to mean "this statement answers
    the question" - it does not. Measured on a labelled probe set before this
    check existed: 25 of 56 questions got a CONFIDENT WRONG number, e.g.

        "How many faculty in the Medicine department are from Deemed
         universities?"          -> 1,046 (every Medicine row; truth 154)
        "How many faculty are not in the Engineering department?"
                                 -> 2,073 (the complement; truth 10,927)
        "How many Assistant Professor faculty are in the Computer Science
         department?"            -> 5,057 (every Assistant Professor; truth 735)

    The last one is the capacity test's own question pool. These answers skip
    the model and the verifier entirely, which is exactly why they must be
    right: a wrong fast-path figure arrives with nothing between it and the
    reader. Declining costs a slow answer. Guessing costs a wrong one.
    """
    lowered = question.lower()
    # Blank out what the family consumed, so the rest can be inspected.
    chars = list(lowered)
    for start, end in list(spans) + list(extra_spans):
        for i in range(start, end):
            chars[i] = " "
    rest = "".join(chars)
    # "learning management system" names the LMS, never the Management
    # department - remove it before looking for unconsumed department names.
    rest = _LEARNING_MANAGEMENT.sub(" ", rest)

    # 1. Any OTHER value from the vocabulary - a second department, a rank the
    #    family has no slot for - including its plural ("Professors").
    for values in vocab.values():
        for value in values:
            if re.search(rf"\b{re.escape(value.lower())}(?:s|es)?\b", rest):
                return value

    # 2. Operators that change the set being counted.
    symbol = _RISKY_SYMBOLS.search(rest)
    if symbol:
        return symbol.group(0)

    # 3. Every remaining word must be filler. Digits never are.
    for word in re.findall(r"[a-z0-9']+", rest):
        if word not in _FILLER:
            return word
    return None


# The dimensions a count or an average may be narrowed by, and how each reads
# in an answer. Column names here are an allowlist in code, like the families;
# only the VALUES come from the question, and only after they are found in the
# database's own vocabulary. They are bound as parameters, never formatted in.
_FILTERS = [
    ("department", "in the {v} department"),
    ("academic_rank", "at the rank of {v}"),
    ("competency_level", "at {v} competency level"),
    ("university_type", "from {v} universities"),
    ("target", "assessed as {v}"),
    ("lms_usage_frequency", "whose LMS usage is recorded as {v}"),
]


def _resolve_filters(question, vocab, taken=()):
    """Every filter value the question names, AT MOST ONE per column.

    Returns ([(column, value)], spans). A second value for the same column
    ("Engineering or Medicine", "Engineering and Medicine combined") is left
    unconsumed on purpose: _unanswered_constraint then sees it and the question
    is declined, because one equality per column cannot express it.

    Plurals are accepted ("for Professors", "Lecturers"), longest value first so
    "Assistant Professors" is not read as "Professor", and spans already claimed
    by another column (or by `taken`, e.g. the metric phrase) are skipped.
    """
    lowered = question.lower()
    consumed = list(taken)
    filters = []
    for column, _phrase in _FILTERS:
        candidates = vocab.get(("faculty_development", column)) or []
        best = None
        for candidate in sorted(candidates, key=len, reverse=True):
            for m in re.finditer(rf"\b{re.escape(candidate.lower())}(?:s|es)?\b", lowered):
                span = m.span()
                if any(span[0] < e and s < span[1] for s, e in consumed):
                    continue
                best = (candidate, span)
                break
            if best:
                break
        if best:
            filters.append((column, best[0]))
            consumed.append(best[1])
    return filters, consumed[len(taken):]


def _describe(filters):
    lookup = dict(_FILTERS)
    return " ".join(lookup[column].format(v=value) for column, value in filters)


def _run_scalar(conn_factory, sql_template, filters):
    """Execute `sql_template` with one bound equality per filter. Returns the
    single value, or raises - the caller turns any failure into None."""
    table = schema_map.physical(intents_table(), qualified=True)
    with conn_factory() as conn:
        placeholder = _placeholder_style(conn)
        where = " AND ".join(
            f"{schema_map.physical_column('faculty_development', column)} = {placeholder}"
            for column, _value in filters
        )
        sql = sql_template.format(table=table) + (f" WHERE {where}" if where else "")
        params = tuple(value for _column, value in filters)
        cur = conn.cursor()
        try:
            cur.execute(sql, params)
            row = cur.fetchone()
        finally:
            try:
                cur.close()
            except Exception:
                pass
    return sql, params, row


def _try_average(question, vocab, conn_factory):
    """"What is the average teaching effectiveness score?" and its relatives.

    Handled apart from the counting families because the rendering differs — an
    average is not an integer, and "67.19999999" is not an answer.

    The phrase -> column mapping is explicit and small (see AVERAGE_METRICS).
    Longest phrase first, so "research publications" is not resolved by a
    shorter overlapping key. A metric that is not listed goes to the model
    rather than being guessed at: "research productivity score" and "research
    publications" are different columns, and a fuzzy match between them returns
    a plausible wrong number.
    """
    if not _AVERAGE_RE.search(question):
        return None

    lowered = question.lower()
    metric = None
    for phrase in sorted(AVERAGE_METRICS, key=len, reverse=True):
        # WORD BOUNDARIES, not `phrase in lowered`.
        #
        # "age" is a substring of "average", so a plain containment test matched
        # the age column for EVERY question containing the word "average".
        # Longest-first ordering hid it whenever a real metric was named, and it
        # surfaced only on an unlisted metric — caught by
        # test_an_unlisted_metric_falls_through, which is what that test is for.
        found = re.search(rf"\b{re.escape(phrase)}\b", lowered)
        if found:
            metric = (phrase,) + AVERAGE_METRICS[phrase]
            metric_span = found.span()
            break
    if metric is None:
        return None
    _phrase, column, noun, places = metric

    # Optional filters, so "average teaching effectiveness in Engineering" is
    # answered about Engineering rather than the whole college - which would be
    # a wrong answer wearing the right words. Any combination of the _FILTERS
    # dimensions ("for Professors", "in Deemed universities"); replayed traffic
    # asked "average AI tool adoption score for Professors" six times and got
    # the college-wide mean every time before this.
    filters, spans = _resolve_filters(question, vocab, taken=[metric_span])
    leftover = _unanswered_constraint(question, [metric_span] + spans, vocab)
    if leftover is not None:
        logger.info("fast path DECLINED average: question also says %r", leftover)
        return None

    physical_column = schema_map.physical_column("faculty_development", column)
    started = time.perf_counter()
    try:
        # Column comes from AVERAGE_METRICS, an allowlist in code. Only the
        # filter VALUES are bound.
        sql, params, row = _run_scalar(
            conn_factory, f"SELECT AVG(CAST({physical_column} AS FLOAT)) AS a FROM {{table}}", filters
        )
    except Exception as exc:
        logger.warning("fast path average failed (%s) — deferring to the model", exc)
        return None

    if row is None or row[0] is None:
        # No rows matched, so there is no average. Not renderable as 0 — a mean
        # of nothing is not zero — so this goes to the model.
        return None

    value = round(float(row[0]), places)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if not filters:
        where = " across the college"
    elif len(filters) == 1 and filters[0][0] == "department":
        where = f" in the {filters[0][1]} department"
    else:
        where = f" for faculty {_describe(filters)}"
    text = f"The average {noun}{where} is {value:,.{places}f}."
    logger.info("fast path HIT intent=average_%s filters=%r %.1fms", column, filters, elapsed_ms)
    return FastAnswer(
        text=text, intent=f"average_{column}", sql=sql, params=params,
        rows=[{"a": value}], elapsed_ms=elapsed_ms,
    )


def _try_filtered_count(question, vocab, conn_factory):
    """A count narrowed by ANY combination of the _FILTERS dimensions.

    Reached only when no fixed family answers the whole question - e.g.
    "How many faculty in the Medicine department are from Deemed
    universities?" (154), which every two-slot family would either miss or,
    before _unanswered_constraint, answer with one condition dropped (1,046).
    The completeness rule is the same: every value consumed, every other word
    filler, otherwise None.
    """
    if not _GENERIC_COUNT_RE.search(question):
        return None
    filters, spans = _resolve_filters(question, vocab)
    leftover = _unanswered_constraint(question, spans, vocab)
    if leftover is not None:
        logger.info("fast path DECLINED filtered count: question also says %r", leftover)
        return None
    started = time.perf_counter()
    try:
        sql, params, row = _run_scalar(conn_factory, "SELECT COUNT(*) AS n FROM {table}", filters)
    except Exception as exc:
        logger.warning("fast path filtered count failed (%s) — deferring to the model", exc)
        return None
    if row is None or row[0] is None:
        return None
    count = int(row[0])
    elapsed_ms = (time.perf_counter() - started) * 1000
    if not filters:
        text = (f"There are {count:,} faculty in the college records." if count
                else "The faculty records are empty.")
    elif count:
        text = f"There are {count:,} faculty {_describe(filters)}."
    else:
        text = f"No faculty in the college records are {_describe(filters)}."
    logger.info("fast path HIT intent=faculty_count_filtered filters=%r n=%s %.1fms",
                filters, count, elapsed_ms)
    return FastAnswer(
        text=text, intent="faculty_count_filtered", sql=sql, params=params,
        rows=[{"n": count}], elapsed_ms=elapsed_ms,
    )


def intents_table():
    """The logical table the averages are computed over."""
    return "faculty_development"


def _placeholder_style(conn):
    """psycopg2 wants %s, pyodbc wants ?. Asked of the driver, not assumed."""
    module = type(conn).__module__ or ""
    return "?" if "pyodbc" in module else "%s"


def try_answer(question, conn_factory):
    """A deterministic answer, or None to let the normal pipeline run."""
    if not question or not question.strip():
        return None

    vocab = entities.vocabulary(conn_factory)
    if vocab is None:
        # UNAVAILABLE, not empty. Deferring to the model is the correct
        # degradation; answering from an empty vocabulary would not be.
        return None

    average = _try_average(question, vocab, conn_factory)
    if average is not None:
        return average
    if _AVERAGE_RE.search(question):
        # An average the average path declined must not be answered by a
        # COUNT family that happens to match the rest of the sentence.
        return None

    matched = [i for i in INTENTS if i.pattern.search(question)]
    if not matched:
        return _try_filtered_count(question, vocab, conn_factory)

    # INTENTS is sorted by slot count descending, so the most specific family
    # that matches wins. A question naming a department AND a rank must not be
    # answered by the department-only family, which would silently drop half of
    # what was asked and return a larger, wrong number.
    for intent in matched:
        resolved = _resolve(question, intent.slots, vocab)
        if resolved is None:
            continue
        values, spans = resolved
        leftover = _unanswered_constraint(question, spans, vocab)
        if leftover is not None:
            # The most specific family that fills its slots has been found and
            # it does not cover the question. A LESS specific family would
            # cover even less of it, so stop here rather than keep looking.
            logger.info(
                "fast path DECLINED intent=%s: question also says %r", intent.name, leftover
            )
            return _try_filtered_count(question, vocab, conn_factory)

        started = time.perf_counter()
        try:
            table = schema_map.physical(intent.table, qualified=True)
            with conn_factory() as conn:
                placeholder = _placeholder_style(conn)
                sql = intent.sql.format(table=table, p=placeholder)
                cur = conn.cursor()
                try:
                    cur.execute(sql, tuple(values))
                    row = cur.fetchone()
                finally:
                    try:
                        cur.close()
                    except Exception:
                        pass
        except Exception as exc:
            # A FAILED LOOKUP IS NOT AN ABSENCE OF RECORDS.
            #
            # Returning None hands the question to the normal pipeline, which
            # has its own degradation path and will tell the user the lookup
            # failed. What must never happen is rendering "There are 0", which
            # would state as fact something we did not learn.
            logger.warning(
                "fast path intent=%s failed (%s) — deferring to the model", intent.name, exc
            )
            return None

        if row is None or row[0] is None:
            return None

        count = int(row[0])
        elapsed_ms = (time.perf_counter() - started) * 1000
        fields = {"n": count}
        for i, value in enumerate(values):
            fields[f"v{i}"] = value
        template = intent.template if count else intent.zero_template
        text = template.format(**fields)

        logger.info(
            "fast path HIT intent=%s values=%r n=%s %.1fms",
            intent.name, values, count, elapsed_ms,
        )
        return FastAnswer(
            text=text,
            intent=intent.name,
            sql=sql,
            params=tuple(values),
            rows=[{"n": count}],
            elapsed_ms=elapsed_ms,
        )

    return _try_filtered_count(question, vocab, conn_factory)
