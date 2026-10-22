"""Single-page, YAML-driven Microsoft SQL Server quiz.

Run:
    pip install streamlit pyyaml
    streamlit run sql_quiz_app.py

Keep sql_50_questions_answers.yaml in the same directory, or upload another
YAML quiz from the app sidebar.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from pathlib import Path

import streamlit as st
import yaml


st.set_page_config(
    page_title="SQL Knowledge Quiz",
    page_icon="🗄️",
    layout="wide",
    initial_sidebar_state="expanded",
)

DEFAULT_YAML = Path(__file__).with_name("spark_sql_50_questions_answers.yaml")
TYPE_LABELS = {
    "multiple_choice": "Multiple choice",
    "multiple_select": "Select all that apply",
    "true_false": "True / False",
    "short_answer": "Short answer",
    "code_completion": "Complete the SQL",
    "query_writing": "Write a SQL query",
    "ordering": "Put the steps in order",
}


def load_quiz(raw: bytes) -> dict:
    """Parse and validate the small part of the schema this app needs."""
    quiz = yaml.safe_load(raw)
    if not isinstance(quiz, dict) or not isinstance(quiz.get("questions"), list):
        raise ValueError("Expected YAML with a top-level 'questions' list.")
    if not quiz["questions"]:
        raise ValueError("The YAML file contains no questions.")
    ids = []
    for i, question in enumerate(quiz["questions"], start=1):
        required = {"id", "type", "question", "answer"}
        if not isinstance(question, dict) or required - question.keys():
            raise ValueError(f"Question #{i} needs these fields: {sorted(required)}")
        if question["type"] not in TYPE_LABELS:
            raise ValueError(f"Unsupported question type: {question['type']}")
        if question["type"] in {"multiple_choice", "multiple_select", "ordering"}:
            if not isinstance(question.get("options"), dict) or not question["options"]:
                raise ValueError(f"Question {question['id']} needs an 'options' mapping.")
        ids.append(str(question["id"]))
    if len(ids) != len(set(ids)):
        raise ValueError("Question IDs must be unique.")
    return quiz


def reset_answers() -> None:
    """Clear all answers and grades but retain uploaded quiz and filters."""
    for key in list(st.session_state):
        if key.startswith(("answer_", "check_", "order_")):
            del st.session_state[key]
    st.session_state["evaluations"] = {}


def mark_dirty(qid: str) -> None:
    """Invalidate a grade when its answer changes."""
    st.session_state["evaluations"].pop(qid, None)


def grade(qid: str, status: str) -> None:
    st.session_state["evaluations"][qid] = status


def simple_norm(value: object) -> str:
    return re.sub(r"\s+", " ", str(value).strip().rstrip(";")).casefold()


def sql_norm(value: object) -> str:
    """Lightweight string comparison, NOT a SQL parser or execution engine."""
    text = simple_norm(value)
    return re.sub(r"\s*([(),=<>+*])\s*", r"\1", text)


def is_correct(question: dict, response: object) -> bool:
    qtype = question["type"]
    answer = question["answer"]
    if qtype == "multiple_select":
        return set(response) == set(answer)
    if qtype == "ordering":
        return list(response) == list(answer)
    if qtype == "true_false":
        return response is answer  # both are booleans
    if qtype == "short_answer":
        accepted = question.get("accepted_answers", [answer])
        return simple_norm(response) in {simple_norm(a) for a in accepted}
    if qtype == "code_completion":
        return sql_norm(response) == sql_norm(answer)
    return response == answer


def get_response(question: dict) -> tuple[object, bool, str]:
    """Render the relevant Streamlit input and return (answer, valid, note)."""
    qid = str(question["id"])
    qtype = question["type"]
    options = question.get("options", {})

    if qtype == "multiple_choice":
        value = st.radio(
            "Select one answer",
            options=list(options.keys()),
            format_func=lambda k: f"{k}. {options[k]}",
            index=None,
            key=f"answer_{qid}",
            on_change=mark_dirty,
            args=(qid,),
        )
        return value, value is not None, "Choose an option first."

    if qtype == "multiple_select":
        st.caption("Select every correct option.")
        selected = []
        for k, option in options.items():
            checked = st.checkbox(
                f"{k}. {option}",
                key=f"check_{qid}_{k}",
                on_change=mark_dirty,
                args=(qid,),
            )
            if checked:
                selected.append(k)
        return selected, bool(selected), "Select at least one option."

    if qtype == "true_false":
        value = st.radio(
            "Choose True or False",
            options=["True", "False"],
            index=None,
            horizontal=True,
            key=f"answer_{qid}",
            on_change=mark_dirty,
            args=(qid,),
        )
        return (value == "True" if value is not None else None), value is not None, "Choose True or False."

    if qtype in {"short_answer", "code_completion"}:
        label = "Your answer" if qtype == "short_answer" else "Missing T-SQL fragment"
        value = st.text_input(
            label,
            placeholder="Type your answer here",
            key=f"answer_{qid}",
            on_change=mark_dirty,
            args=(qid,),
        )
        return value, bool(value.strip()), "Enter your answer first."

    if qtype == "query_writing":
        value = st.text_area(
            "Write your T-SQL solution",
            height=180,
            placeholder="SELECT ...",
            key=f"answer_{qid}",
            on_change=mark_dirty,
            args=(qid,),
        )
        return value, bool(value.strip()), "Write some SQL before reviewing."

    if qtype == "ordering":
        st.caption("Assign a different item to each step, from first to last.")
        labels = list(options)
        selected = []
        for step in range(1, len(labels) + 1):
            choice = st.selectbox(
                f"Step {step}",
                options=[""] + labels,
                format_func=lambda k: f"{k}. {options[k]}" if k else "— Select an item —",
                key=f"order_{qid}_{step}",
                on_change=mark_dirty,
                args=(qid,),
            )
            selected.append(choice)
        if any(x == "" for x in selected):
            return selected, False, "Fill all positions before checking."
        if len(set(selected)) != len(selected):
            return selected, False, "Use every option exactly once (no duplicates)."
        return selected, True, ""

    raise AssertionError("Unknown question type")


def display_answer(question: dict) -> None:
    """Display a formatted answer, explanation, and documentation."""
    qtype, answer = question["type"], question["answer"]
    st.markdown("**Reference answer**")
    if qtype in {"query_writing", "code_completion"}:
        st.code(answer, language="sql")
    elif qtype == "true_false":
        st.write("True" if answer else "False")
    elif qtype == "ordering":
        for idx, key in enumerate(answer, start=1):
            st.write(f"{idx}. {key}: {question['options'][key]}")
    elif qtype in {"multiple_choice", "multiple_select"}:
        for key in (answer if isinstance(answer, list) else [answer]):
            st.write(f"**{key}.** {question['options'][key]}")
    else:
        st.write(answer)

    st.markdown("**Explanation**")
    st.write(question.get("explanation", "No explanation supplied."))

    if question.get("grading_notes"):
        st.caption(f"Grading notes: {question['grading_notes']}")

    docs = question.get("documentation", [])
    if docs:
        st.markdown("**Documentation**")
        for entry in docs:
            title, url = entry.get("title", "Read documentation"), entry.get("url", "")
            if url.startswith(("https://", "http://")):
                st.markdown(f"- [{title}]({url})")


def move_question(direction: int, visible_ids: list[str]) -> None:
    current = st.session_state.get("question_select", visible_ids[0])
    idx = visible_ids.index(current)
    st.session_state["question_select"] = visible_ids[max(0, min(len(visible_ids) - 1, idx + direction))]


def csv_results(questions: list[dict], evaluations: dict) -> str:
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Question ID", "Topic", "Difficulty", "Type", "Result"])
    for question in questions:
        writer.writerow([
            question["id"],
            question.get("topic", ""),
            question.get("difficulty", ""),
            question["type"],
            evaluations.get(str(question["id"]), "not_attempted"),
        ])
    return output.getvalue()


# --------------------------- Quiz loading ---------------------------
st.title("🗄️ SQL Knowledge Quiz")
st.caption("Interactive Microsoft SQL Server (T-SQL) practice — one question at a time.")

uploaded = st.sidebar.file_uploader("Optional: upload a quiz YAML", type=["yaml", "yml"])
try:
    if uploaded is not None:
        raw_yaml = uploaded.getvalue()
        source_name = uploaded.name
    else:
        raw_yaml = DEFAULT_YAML.read_bytes()
        source_name = DEFAULT_YAML.name
    quiz = load_quiz(raw_yaml)
except (OSError, ValueError, yaml.YAMLError) as exc:
    st.error(f"Unable to load quiz: {exc}")
    st.info("Place sql_50_questions_answers.yaml alongside this Python file, or upload a YAML file from the sidebar.")
    st.stop()

source_id = hashlib.sha256(raw_yaml).hexdigest()
if st.session_state.get("source_id") != source_id:
    reset_answers()
    st.session_state.pop("question_select", None)
    st.session_state["source_id"] = source_id

questions = quiz["questions"]
by_id = {str(q["id"]): q for q in questions}
evaluations = st.session_state.setdefault("evaluations", {})
metadata = quiz.get("metadata", {})

# -------------------------- Sidebar filters -------------------------
st.sidebar.markdown(f"**Loaded:** {source_name}")
st.sidebar.caption(f"{len(questions)} questions")
difficulties = ["All"] + sorted({str(q.get("difficulty", "unspecified")) for q in questions})
topics = ["All"] + sorted({str(q.get("topic", "Other")) for q in questions})
types = ["All"] + sorted({q["type"] for q in questions})

chosen_difficulty = st.sidebar.selectbox("Difficulty", difficulties)
chosen_topic = st.sidebar.selectbox("Topic", topics)
chosen_type = st.sidebar.selectbox("Question type", types, format_func=lambda t: TYPE_LABELS.get(t, t))
visible = [
    str(q["id"]) for q in questions
    if (chosen_difficulty == "All" or q.get("difficulty", "unspecified") == chosen_difficulty)
    and (chosen_topic == "All" or q.get("topic", "Other") == chosen_topic)
    and (chosen_type == "All" or q["type"] == chosen_type)
]

if not visible:
    st.info("No questions match your selected filters.")
    st.stop()

if st.session_state.get("question_select") not in visible:
    st.session_state["question_select"] = visible[0]

qid = st.sidebar.selectbox(
    "Jump to question",
    options=visible,
    format_func=lambda key: f"{key} — {by_id[key].get('topic', 'Other')}",
    key="question_select",
)

st.sidebar.button("Reset all answers", on_click=reset_answers, use_container_width=True)
st.sidebar.download_button(
    "Export results (CSV)",
    data=csv_results(questions, evaluations),
    file_name="sql_quiz_results.csv",
    mime="text/csv",
    use_container_width=True,
)
st.sidebar.caption("Your answers are retained during this Streamlit session. SQL-writing questions are self-graded.")

# -------------------------- Progress summary ------------------------
correct = sum(status == "correct" for status in evaluations.values())
incorrect = sum(status == "incorrect" for status in evaluations.values())
completed = correct + incorrect
accuracy = (100 * correct / completed) if completed else 0.0

col1, col2, col3, col4 = st.columns(4)
col1.metric("Completed", f"{completed}/{len(questions)}")
col2.metric("Correct", correct)
col3.metric("Incorrect", incorrect)
col4.metric("Accuracy", f"{accuracy:.0f}%" if completed else "—")
st.progress(completed / len(questions))

# --------------------------- Question view --------------------------
question = by_id[qid]
position = visible.index(qid)
head1, head2 = st.columns([4, 1])
with head1:
    st.subheader(f"Question {position + 1} of {len(visible)}")
    st.caption(f"{qid}  •  {question.get('topic', 'SQL')}  •  {question.get('difficulty', 'Unknown').title()}  •  {TYPE_LABELS[question['type']]}")
with head2:
    status = evaluations.get(qid)
    if status == "correct":
        st.success("Correct ✓")
    elif status == "incorrect":
        st.error("Incorrect ✗")
    elif status in {"review", "revealed"}:
        st.info("Not scored")

st.markdown(f"**{question['question']}**")
response, valid, note = get_response(question)

status = evaluations.get(qid)
st.write("")
if status is None:
    check_label = "Compare with sample answer" if question["type"] == "query_writing" else "Check answer"
    left, right = st.columns([1, 3])
    with left:
        if st.button(check_label, type="primary", disabled=not valid, use_container_width=True):
            if question["type"] == "query_writing":
                grade(qid, "review")
            else:
                grade(qid, "correct" if is_correct(question, response) else "incorrect")
            st.rerun()
    with right:
        if st.button("Reveal answer (not scored)"):
            grade(qid, "revealed")
            st.rerun()
    if not valid:
        st.caption(note)
else:
    if status == "review":
        st.info("Compare your SQL with the reference solution. Equivalent SQL can be correct even if it looks different.")
    elif status == "revealed":
        st.info("Answer revealed. This question is not included in your score.")
    elif status == "incorrect":
        st.error("Not quite. Compare your response with the answer and explanation below.")
    elif status == "correct":
        st.success("Nice work! Your answer is recorded as correct.")

    with st.container(border=True):
        display_answer(question)

    if status == "review":
        a, b = st.columns(2)
        a.button("✓ My SQL is correct", type="primary", on_click=grade, args=(qid, "correct"), use_container_width=True)
        b.button("✗ Needs more practice", on_click=grade, args=(qid, "incorrect"), use_container_width=True)
    elif status == "incorrect" and question["type"] == "code_completion":
        st.caption("Automatic checking compares normalized text, not SQL meaning. An equivalent expression may be accepted manually.")
        st.button("Mark my SQL as equivalent", on_click=grade, args=(qid, "correct"))
    elif status == "revealed":
        st.caption("Edit your answer to try again and receive a score.")

st.divider()
prev_col, next_col = st.columns(2)
prev_col.button(
    "← Previous question",
    disabled=position == 0,
    on_click=move_question,
    args=(-1, visible),
    use_container_width=True,
)
next_col.button(
    "Next question →",
    disabled=position == len(visible) - 1,
    on_click=move_question,
    args=(1, visible),
    use_container_width=True,
)

with st.expander("Reference database schema"):
    st.caption("The illustrative tables assumed by some quiz questions.")
    for table_name, columns in metadata.get("reference_schema", {}).items():
        st.code(f"{table_name} ({columns})", language="sql")

with st.expander("Results by question"):
    for q in questions:
        state = evaluations.get(str(q["id"]), "not_attempted")
        symbol = {"correct": "✓", "incorrect": "✗", "review": "◌", "revealed": "◌"}.get(state, "·")
        st.write(f"{symbol} {q['id']} — {q.get('topic', 'SQL')}: {state.replace('_', ' ')}")
