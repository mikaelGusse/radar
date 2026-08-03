#!/usr/bin/env python3
"""Generate many synthetic "garbage" submissions for load testing.

Usage examples:
  python util/generate_garbage_submissions.py --course coursec --students 300 --submissions-per-student 3
    python util/generate_garbage_submissions.py --course coursec --create-exercises 5 --students 200 --set-filesystem-provider
    python util/generate_garbage_submissions.py --course coursec --create-exercises 3 --include-existing-exercises --students 150
    python util/generate_garbage_submissions.py --course coursec --students 800 --submissions-per-student 2 --run-matching

Notes:
- This script is intended for local/dev test data generation.
- For Dolos report generation from these submissions, the course provider should
  be "filesystem" so Radar reads local submission files.
- By default submissions are prepared with Radar's tokenizer pipeline; use
    --skip-prepare to only insert raw rows/files.
- With --create-exercises N, the script creates N brand new synthetic
    exercises and uses those by default.
"""

from __future__ import annotations

import argparse
import datetime
import os
import random
import string
import sys
from typing import List
import secrets


REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "radar.settings")

import django  # noqa: E402

django.setup()

from aplus_client.django.models import ApiNamespace  # noqa: E402
from data import files  # noqa: E402
from data.models import Course, Exercise, Student, Submission  # noqa: E402
from matcher.tasks import match_exercise  # noqa: E402
from provider.insert import InsertError, prepare_submission  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate synthetic submissions for a course")
    parser.add_argument("--course", required=True, help="Course key (e.g. coursec)")
    parser.add_argument(
        "--students",
        type=int,
        default=200,
        help="How many synthetic students to create (default: 200)",
    )
    parser.add_argument(
        "--submissions-per-student",
        type=int,
        default=2,
        help="How many submissions to create per student per exercise (default: 2)",
    )
    parser.add_argument(
        "--create-exercises",
        type=int,
        default=0,
        help="Create N new synthetic exercises (default: 0)",
    )
    parser.add_argument(
        "--exercise-prefix",
        default="garbageex",
        help="Key prefix for created synthetic exercises (default: garbageex)",
    )
    parser.add_argument(
        "--include-existing-exercises",
        action="store_true",
        help="When used with --create-exercises, also generate submissions for pre-existing course exercises",
    )
    parser.add_argument(
        "--student-prefix",
        default="garbagestudent",
        help="Prefix for synthetic student keys (default: garbagestudent)",
    )
    parser.add_argument(
        "--submission-prefix",
        default="garbagesub",
        help="Prefix for synthetic submission keys (default: garbagesub)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=1337,
        help="Random seed for reproducible data (default: 1337)",
    )
    parser.add_argument(
        "--output-profile",
        choices=("varied", "clustered", "chaotic"),
        default="varied",
        help="How similar the generated submissions should be (default: varied)",
    )
    parser.add_argument(
        "--set-filesystem-provider",
        action="store_true",
        help="Set course provider to filesystem so generated local files are used by Radar",
    )
    parser.add_argument(
        "--delete-existing-generated",
        action="store_true",
        help="Delete previously generated synthetic submissions/students for the chosen prefixes",
    )
    parser.add_argument(
        "--skip-prepare",
        action="store_true",
        help="Only create DB rows + source files, skip Radar tokenization/prepare pipeline",
    )
    parser.add_argument(
        "--run-matching",
        action="store_true",
        help="Run legacy matcher for touched exercises after generation (can be very slow)",
    )
    return parser.parse_args()


def build_source_blob(
    rng: random.Random,
    cluster_id: int,
    exercise_key: str,
    student_key: str,
    attempt: int,
    exercise_index: int,
    student_index: int,
    output_profile: str,
) -> str:
    """Build synthetic source with a controllable similarity profile."""

    shared_blocks = [
        """
def shared_sum(values):
    total = 0
    for value in values:
        total += value
    return total
""",
        """
def filter_even(values):
    result = []
    for value in values:
        if value % 2 == 0:
            result.append(value)
    return result
""",
        """
class Counter:
    def __init__(self):
        self.value = 0

    def add(self, amount):
        self.value += amount
""",
        """
def normalize(text):
    return text.strip().lower()
""",
    ]

    exercise_blocks = [
        f"""
def solve_{exercise_index}(items):
    items = list(items)
    items.sort()
    return items
""",
        f"""
def score_{exercise_index}(values):
    score = 0
    for value in values:
        score += (value % 7)
    return score
""",
        f"""
def map_{exercise_index}(items):
    return {{item: idx for idx, item in enumerate(items)}}
""",
    ]

    # Use a handful of clusters for strong similarity, but vary the family and
    # the per-student mutations so we get a mix of strong/weak/near-unique rows.
    family = (cluster_id + exercise_index) % len(shared_blocks)
    shared = shared_blocks[family].strip()
    exercise_shared = exercise_blocks[exercise_index % len(exercise_blocks)].strip()

    if output_profile == "clustered":
        noise_count = 6
        extra_unique = 0
    elif output_profile == "chaotic":
        noise_count = 24
        extra_unique = 12
    else:
        noise_count = 12
        extra_unique = 4

    # Deterministically vary the amount of shared content per student so the
    # Students view gets a spread of similarities.
    student_mode = (student_index + exercise_index) % 6
    if student_mode == 0:
        shared_parts = [shared, exercise_shared]
    elif student_mode == 1:
        shared_parts = [shared]
    elif student_mode == 2:
        shared_parts = [exercise_shared]
    elif student_mode == 3:
        shared_parts = [shared, exercise_shared, "\n".join(["# tiny helper", "def helper(x):", "    return x + 1"])]
    elif student_mode == 4:
        shared_parts = [shared, "\n".join(["# alternate branch", "def branch(x):", "    return x * 2"])]
    else:
        shared_parts = [shared]

    if output_profile == "chaotic" and (student_index % 5 == 0):
        shared_parts = []

    noise_lines = []
    for idx in range(noise_count):
        token = "".join(rng.choices(string.ascii_lowercase, k=8))
        value = rng.randint(0, 99999)
        noise_lines.append(f"{token}_{idx} = {value}")

    unique_lines = []
    for idx in range(extra_unique):
        token = "".join(rng.choices(string.ascii_lowercase, k=10))
        unique_lines.append(f"def unique_{exercise_index}_{student_index}_{attempt}_{idx}(x): return x + {rng.randint(1, 9)}")
        unique_lines.append(f"{token} = {rng.randint(0, 99999)}")

    footer = (
        f"# exercise={exercise_key} student={student_key} attempt={attempt} "
        f"cluster={cluster_id} profile={output_profile}"
    )

    parts = [*shared_parts, *noise_lines, *unique_lines, footer]
    return "\n".join([part.strip() for part in parts if part.strip()] + [""])


def make_unique_submission_key(prefix: str, run_tag: str) -> str:
    """Build a short, collision-safe submission key (max 64 chars)."""
    for _ in range(10):
        token = secrets.token_hex(5)  # 10 lowercase hex chars
        key = f"{prefix}{run_tag}{token}"
        if len(key) > 64:
            key = key[:64]
        if not Submission.objects.filter(key=key).exists():
            return key
    raise RuntimeError("Failed to generate a unique submission key after multiple attempts")


def ensure_exercises(course: Course, create_count: int, exercise_prefix: str) -> List[Exercise]:
    exercises = list(course.exercises.all().order_by("key"))
    if exercises:
        return exercises

    if create_count <= 0:
        raise SystemExit(
            "Course has no exercises. Provide --create-exercises N to create synthetic exercises."
        )

    created = []
    for idx in range(1, create_count + 1):
        key = f"{exercise_prefix}{idx:03d}"
        ex, _ = Exercise.objects.get_or_create(
            course=course,
            key=key,
            defaults={"name": f"Garbage Exercise {idx}"},
        )
        created.append(ex)
    return created


def create_synthetic_exercises(course: Course, create_count: int, exercise_prefix: str) -> List[Exercise]:
    """Create exactly create_count new synthetic exercises by appending indexes.

    If exercise keys already exist for this prefix, continue from the max
    numeric suffix instead of reusing old keys.
    """
    if create_count <= 0:
        return []

    existing_keys = set(course.exercises.values_list("key", flat=True))
    max_suffix = 0
    for key in existing_keys:
        if not key.startswith(exercise_prefix):
            continue
        suffix = key[len(exercise_prefix):]
        if suffix.isdigit():
            max_suffix = max(max_suffix, int(suffix))

    created = []
    next_suffix = max_suffix
    for _ in range(create_count):
        next_suffix += 1
        # Use 3+ digits to keep sort order stable and readable.
        key = f"{exercise_prefix}{next_suffix:03d}"
        while key in existing_keys:
            next_suffix += 1
            key = f"{exercise_prefix}{next_suffix:03d}"
        ex = Exercise.objects.create(course=course, key=key, name=f"Garbage Exercise {next_suffix}")
        existing_keys.add(key)
        created.append(ex)

    return created


def main() -> None:
    args = parse_args()
    rng = random.Random(args.seed)

    course = Course.objects.filter(key=args.course).first()
    course_created = False
    if course is None:
        namespace, _ = ApiNamespace.objects.get_or_create(id=0)
        course = Course(
            key=args.course,
            name=args.course,
            provider="filesystem",
            tokenizer="python",
            api_id=0,
            namespace=namespace,
        )
        course.save()
        course_created = True
        print(f"Created course {course.key}")

    if args.set_filesystem_provider and course.provider != "filesystem":
        course.provider = "filesystem"
        course.save(update_fields=["provider"])
        print(f"Updated course provider to filesystem for {course.key}")

    if course.tokenizer != "python" and course_created:
        course.tokenizer = "python"
        course.save(update_fields=["tokenizer"])
        print(f"Updated course tokenizer to python for {course.key}")

    created_exercises = create_synthetic_exercises(course, args.create_exercises, args.exercise_prefix)

    if args.create_exercises > 0:
        if args.include_existing_exercises:
            exercises = list(course.exercises.all().order_by("key"))
        else:
            exercises = created_exercises
    else:
        exercises = list(course.exercises.all().order_by("key"))

    if not exercises:
        exercises = ensure_exercises(course, args.create_exercises, args.exercise_prefix)

    if course.provider != "filesystem":
        print(
            "Warning: course provider is not 'filesystem'. Dolos/source reads may ignore generated local files. "
            "Use --set-filesystem-provider for fully local test data."
        )

    should_prepare = not args.skip_prepare
    if should_prepare and course.provider != "filesystem":
        print(
            "Disabling prepare pipeline because provider is not filesystem. "
            "Use --set-filesystem-provider (or --skip-prepare explicitly)."
        )
        should_prepare = False

    if args.delete_existing_generated:
        deleted_subs, _ = Submission.objects.filter(
            exercise__course=course,
            key__startswith=args.submission_prefix,
        ).delete()
        deleted_students, _ = Student.objects.filter(
            course=course,
            key__startswith=args.student_prefix,
        ).delete()
        print(f"Deleted {deleted_subs} old submissions and {deleted_students} old students for prefixes")

    students: List[Student] = []
    for idx in range(1, args.students + 1):
        key = f"{args.student_prefix}{idx:05d}"
        student, _ = Student.objects.get_or_create(
            course=course,
            key=key,
            defaults={
                "name": f"Garbage Student {idx}",
                "email": f"{key}@example.invalid",
                "is_staff": False,
            },
        )
        students.append(student)

    created_count = 0
    prepared_count = 0
    prepare_failed_count = 0
    now = datetime.datetime.now(datetime.timezone.utc)
    run_tag = now.strftime("%y%m%d%H%M%S")

    for exercise in exercises:
        for s_index, student in enumerate(students):
            cluster_id = s_index % 8
            for attempt in range(1, args.submissions_per_student + 1):
                submission_key = make_unique_submission_key(args.submission_prefix, run_tag)
                grade = round(rng.uniform(0, 100), 2)
                submission = Submission.objects.create(
                    key=submission_key,
                    aplus_key=None,
                    exercise=exercise,
                    student=student,
                    provider_url=None,
                    provider_submission_time=now,
                    grade=grade,
                    matched=False,
                    invalid=False,
                    max_similarity=0.0,
                )
                text = build_source_blob(
                    rng=rng,
                    cluster_id=cluster_id,
                    exercise_key=exercise.key,
                    student_key=student.key,
                    attempt=attempt,
                    exercise_index=exercise.id,
                    student_index=s_index,
                    output_profile=args.output_profile,
                )
                files.put_submission_text(submission, text)

                if should_prepare:
                    try:
                        prepare_submission(submission)
                        prepared_count += 1
                    except InsertError:
                        prepare_failed_count += 1
                    except Exception:
                        prepare_failed_count += 1

                created_count += 1

    if args.run_matching:
        for exercise in exercises:
            # Mark these submissions as expected for matching, then run match.
            exercise.touch_all_timestamps()
            match_exercise(exercise.id, delay=False)

    print("Done.")
    print(f"Course: {course.key}")
    print(f"Exercises used: {len(exercises)}")
    if created_exercises:
        print(f"Exercises created this run: {len(created_exercises)}")
    print(f"Students created/reused: {len(students)}")
    print(f"Submissions created: {created_count}")
    print(f"Submissions prepared for Radar: {prepared_count}")
    if prepare_failed_count:
        print(f"Prepare failures: {prepare_failed_count}")
    print(f"Legacy matching executed: {'yes' if args.run_matching else 'no'}")
    print(
        f"Expected total per run: students({args.students}) * exercises({len(exercises)}) * "
        f"submissions-per-student({args.submissions_per_student})"
    )


if __name__ == "__main__":
    main()
