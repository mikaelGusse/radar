"""
Build Dolos datasets from Radar submissions.

Dolos only understands the relationships between submissions through the
``info.csv`` metadata bundled with the ZIP it analyses: each file may carry a
``full_name`` (author), a ``label`` (colour group) and a ``created_at``
(timeline). We additionally encode the structure in the ZIP path
(``course/exercise/...``) so a single report can span many exercises and many
courses.

This module is deliberately free of Django and network imports so the dataset
builder stays runnable on its own (see the ``__main__`` self-check at the
bottom: ``python -m review.dolos_reports``).
"""
import csv
import datetime
import os
import zipfile


INFO_COLUMNS = ["filename", "full_name", "label", "created_at", "exercise", "course"]


# Radar tokenizer -> Dolos language name (see LanguagePicker in
# dolos/lib/src/lib/language.ts). Anything Dolos has no parser for
# (skip/text/html/css/matlab/unknown) falls back to "char", Dolos's
# character-based tokenizer: it analyses any content and never throws.
_DOLOS_LANGUAGES = {
    "python": "python",
    "scala": "scala",
    "c": "c",
    "cpp": "cpp",
    "java": "java",
    "js": "javascript",
}


def dolos_language(tokenizer):
    """Map a Radar tokenizer to a valid Dolos language; unknown -> 'char'."""
    return _DOLOS_LANGUAGES.get(tokenizer, "char")


def submission_path(submission):
    """Relative ZIP path encoding course/exercise/student for a submission."""
    exercise = submission.exercise
    return "/".join(
        [
            exercise.course.key,
            exercise.key,
            "%s_%s.txt" % (submission.student.key, submission.id),
        ]
    )


def write_dataset(work_dir, submissions, label_fn, get_text):
    """
    Write each submission's source into ``work_dir`` under its
    :func:`submission_path` and an ``info.csv`` at the root.

    ``label_fn(submission)`` returns the Dolos colour label and
    ``get_text(submission)`` returns the source code. Returns the info rows.
    """
    rows = []
    for submission in submissions:
        rel_path = submission_path(submission)
        abs_path = os.path.join(work_dir, *rel_path.split("/"))
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as source_file:
            source_file.write(get_text(submission))
        created_at = submission.provider_submission_time
        if isinstance(created_at, datetime.datetime):
            created_at = created_at.strftime("%Y-%m-%d %H:%M:%S %z")
        exercise = submission.exercise
        rows.append(
            {
                "filename": rel_path,
                "full_name": "%s #%s" % (submission.student.display_name, submission.key),
                "label": label_fn(submission),
                "created_at": created_at or "",
                "exercise": exercise.name,
                "course": exercise.course.name,
            }
        )
    with open(os.path.join(work_dir, "info.csv"), "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=INFO_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return rows


def zip_dataset(src_dir, zip_path):
    """Zip ``src_dir`` into ``zip_path`` preserving relative paths (info.csv at root)."""
    with zipfile.ZipFile(zip_path, "w") as zip_handle:
        for root, _dirs, files in os.walk(src_dir):
            for filename in files:
                file_path = os.path.join(root, filename)
                zip_handle.write(file_path, arcname=os.path.relpath(file_path, src_dir))


def _demo():
    """Self-check with fake submissions; run: ``python -m review.dolos_reports``."""
    import tempfile
    import types

    def fake(course_key, ex_key, ex_name, course_name, student_key, sub_id):
        course = types.SimpleNamespace(key=course_key, name=course_name)
        return types.SimpleNamespace(
            id=sub_id,
            key=str(sub_id),
            student=types.SimpleNamespace(key=student_key, display_name=student_key),
            exercise=types.SimpleNamespace(key=ex_key, name=ex_name, course=course),
            provider_submission_time=None,
        )

    submissions = [
        fake("c1", "e1", "Ex 1", "Course 1", "alice", 1),
        fake("c1", "e2", "Ex 2", "Course 1", "alice", 2),
        fake("c1", "e1", "Ex 1", "Course 1", "bob", 3),
    ]
    with tempfile.TemporaryDirectory() as work_dir:
        write_dataset(
            work_dir,
            submissions,
            label_fn=lambda s: s.exercise.name,
            get_text=lambda s: "code",
        )
        for rel in ("c1/e1/alice_1.txt", "c1/e2/alice_2.txt", "c1/e1/bob_3.txt"):
            assert os.path.isfile(os.path.join(work_dir, *rel.split("/"))), rel
        with open(os.path.join(work_dir, "info.csv")) as info:
            reader = csv.DictReader(info)
            assert reader.fieldnames == INFO_COLUMNS, reader.fieldnames
            read_rows = list(reader)
        # Zip outside work_dir so it is not included in itself.
        zip_path = os.path.join(tempfile.gettempdir(), "dolos_reports_demo.zip")
        zip_dataset(work_dir, zip_path)
        with zipfile.ZipFile(zip_path) as archive:
            names = set(archive.namelist())
        os.remove(zip_path)

    assert len(read_rows) == 3, len(read_rows)
    # Alice submitted to two different exercises -> Dolos sees her across exercises.
    alice_labels = {r["label"] for r in read_rows if r["full_name"].startswith("alice")}
    assert alice_labels == {"Ex 1", "Ex 2"}, alice_labels
    assert "info.csv" in names
    assert "c1/e1/alice_1.txt" in names
    # Radar tokenizers must map to names Dolos actually accepts, else the CLI
    # throws (LanguageError) and the report fails with "Oops".
    assert dolos_language("js") == "javascript"
    assert dolos_language("python") == "python"
    assert dolos_language("skip") == "char"
    assert dolos_language("matlab") == "char"
    assert dolos_language(None) == "char"
    print("dolos_reports self-check ok")


if __name__ == "__main__":
    _demo()
