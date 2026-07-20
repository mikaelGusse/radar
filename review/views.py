import datetime
import logging
import json
import mimetypes
import time
from urllib.parse import urljoin
import concurrent.futures
import shutil
import tempfile

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.timezone import now
from django.http.response import JsonResponse, HttpResponse, HttpResponseBadRequest
from django.shortcuts import render, redirect, get_object_or_404
from django.template import loader as template_loader
from celery.result import AsyncResult
from django.views import View
import requests
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.utils.decorators import method_decorator
from django.core.cache import cache

from django.core.handlers.wsgi import WSGIRequest
from django.db.models import Avg, Q, F

from data.models import Course, Comparison, Student, Submission, Exercise
from data import graph
from radar.config import provider_config, configured_function
from radar.settings import (
    DOLOS_API_SERVER_URL, DOLOS_PROXY_API_URL, DOLOS_PROXY_WEB_URL, DOLOS_WEB_SERVER_URL, CELERY_DEBUG
)
from review.decorators import access_resource
from review.forms import ExerciseForm, ExerciseTemplateForm, DeleteExerciseFrom
from review.helpers import handle_async_task, build_clusters_for
from review.dolos_reports import dolos_language, write_dataset, zip_dataset
from util.misc import is_ajax
import zipfile
import os
import csv
import re
import pytz
from django.http import FileResponse
from provider.tasks import recompare_all

# pylint: disable=no-else-return

logger = logging.getLogger("radar.review")


@login_required
def index(request):
    # Default to Legacy Radar when the mode has not been selected yet.
    request.session.setdefault("legacy_radar", True)
    return render(
        request,
        "review/index.html",
        {
            "hierarchy": ((settings.APP_NAME, None),),
            "courses": Course.objects.get_available_courses(request.user),
        },
    )


@login_required
def toggle_radar_mode(request):
    """Flip between Legacy Radar (default) and New Radar (Dolos) navigation."""
    # Toggle from a Legacy default so first-time users switch to New Radar.
    request.session["legacy_radar"] = not request.session.get("legacy_radar", True)
    request.session.save()
    referer = request.META.get("HTTP_REFERER", "")
    # Only bounce back to a same-site page (avoid open redirects).
    if url_has_allowed_host_and_scheme(referer, allowed_hosts={request.get_host()}):
        return redirect(referer)
    return redirect("index")


@access_resource
def course(request, course_key=None, course=None):
    # Legacy Radar (default) shows the classic management table.
    # New Radar (Dolos) is enabled when legacy_radar is False.
    if request.method == "GET" and not request.session.get("legacy_radar", True):
        return redirect("dolos_hub", course_key=course.key)
    context = {
        "hierarchy": ((settings.APP_NAME, reverse("index")), (course.name, None)),
        "course": course,
        "exercises": course.exercises.all(),
    }
    if request.method == "POST":
        # The user can click "Match all unmatched" for a shortcut to match all unmatched submissions for every exercise
        p_config = provider_config(course.provider)

        if "match-all-unmatched-for-exercises" in request.POST:
            configured_function(p_config, 'recompare_unmatched')(course)
            return redirect("course", course_key=course.key)

        if "recompare_all" in request.POST:
            if not settings.DEBUG or CELERY_DEBUG:
                recompare_all.delay(course.id)
            else:
                recompare_all(course.id)
            return redirect("course", course_key=course.key)

    return render(request, "review/course.html", context)


@access_resource
def course_histograms(request, course_key=None, course=None):
    return render(
        request,
        "review/course_histograms.html",
        {
            "hierarchy": (
                (settings.APP_NAME, reverse("index")),
                (course.name, reverse("course", kwargs={"course_key": course.key})),
                ("Histograms", None),
            ),
            "course": course,
            "exercises": course.exercises.all(),
        },
    )


# Render the exercise page (Dolos launcher)
@access_resource
def exercise(
    request: WSGIRequest,
    course_key: str | None = None,
    exercise_key: str | None = None,
    course: Course | None = None,
    exercise: Exercise | None = None
) -> HttpResponse:

    # Legacy Radar (default) shows the classic comparison view.
    # New Radar (Dolos) is enabled when legacy_radar is False.
    if not request.session.get("legacy_radar", True):
        return redirect("dolos_hub_exercise", course_key=course.key, exercise_key=exercise.key)

    rows = int(request.GET.get('rows', settings.SUBMISSION_VIEW_HEIGHT))
    one_pair_per_match = request.GET.get('one_pair_per_match', 'false').lower() == 'true'
    best_submissions = request.GET.get('best_submissions', 'false').lower() == 'true'
    comparisons = exercise.top_comparisons(rows, one_pair_per_match, best_submissions)
    flagged_comparisons = exercise.flagged_comparisons(False)

    return render(
        request,
        "review/exercise.html",
        {
            "hierarchy": (
                (settings.APP_NAME, reverse("index")),
                (course.name, reverse("course", kwargs={"course_key": course.key})),
                (exercise.name, None),
            ),
            "course": course,
            "exercise": exercise,
            "comparisons": comparisons,
            "flagged_comparisons": flagged_comparisons,
        },
    )


@access_resource
def configure_course(request, course_key=None, course=None): #pylint: disable=too-many-branches
    context = {
        "hierarchy": (
            (settings.APP_NAME, reverse("index")),
            (course.name, reverse("course", kwargs={"course_key": course.key})),
            ("Configure", None),
        ),
        "course": course,
        "provider_data": [
            {
                "description": "{:s}, all submission data are retrieved from here".format(
                    course.provider_name
                ),
                "path": settings.PROVIDERS[course.provider].get("host", "UNKNOWN"),
            },
            {
                "description": "Data providers should POST the IDs of new submissions to this path in order to"
                               " have them automatically downloaded by Radar",
                "path": request.build_absolute_uri(
                    reverse("hook_submission", kwargs={"course_key": course.key})
                ),
            },
            {
                "description": "Login requests using the LTI-protocol should be made to this path",
                "path": request.build_absolute_uri(reverse("lti_login")),
            },
        ],
        "errors": [],
    }

    # The state of the API read task is contained in this dict
    pending_api_read = {
        "task_id": None,
        "poll_URL": reverse("configure_course", kwargs={"course_key": course.key}),
        "ready": False,
        "poll_interval_seconds": 5,
        "config_type": "automatic",
    }

    if request.method == "GET":
        if "true" in request.GET.get("success", ''):
            # All done, show success message
            context["change_success"] = True
        pending_api_read["json"] = json.dumps(pending_api_read)
        context["pending_api_read"] = pending_api_read
        return render(request, "review/configure.html", context)

    if request.method != "POST":
        return HttpResponseBadRequest()

    p_config = provider_config(course.provider)

    if "create-exercises" in request.POST or "overwrite-exercises" in request.POST:
        # API data has been fetched in a previous step, now the user wants to add exercises
        # that were shown in the table
        if "create-exercises" in request.POST:
            # Pre-configured, read-only table
            exercises = json.loads(request.POST["exercises-json"])
            for exercise_data in exercises:
                key_str = str(exercise_data["exercise_key"])
                exercise = course.get_exercise(key_str)
                exercise.set_from_config(exercise_data)
                exercise.save()
                # Queue fetch and match for all submissions for this exercise
                full_reload = configured_function(p_config, "full_reload")
                full_reload(exercise, p_config)
        elif "overwrite-exercises" in request.POST:
            # Manual configuration, editable table, overwrite existing
            checked_rows = (
                key.split("-", 1)[0] for key in request.POST if key.endswith("enabled")
            )
            exercises = (
                {
                    "exercise_key": exercise_key,
                    "name": request.POST[exercise_key + "-name"],
                    "template_source": request.POST.get(
                        exercise_key + "-template-source", ''
                    ),
                    "tokenizer": request.POST[exercise_key + "-tokenizer"],
                    "minimum_match_tokens": request.POST[
                        exercise_key + "-min-match-tokens"
                    ],
                }
                for exercise_key in checked_rows
            )
            for exercise_data in exercises:
                key = str(exercise_data["exercise_key"])
                course.exercises.filter(key=key).delete()
                exercise = course.get_exercise(key)
                exercise.set_from_config(exercise_data)
                exercise.save()
                full_reload = configured_function(p_config, "full_reload")
                full_reload(exercise, p_config)
        return redirect(
            reverse("configure_course", kwargs={"course_key": course.key})
            + "?success=true"
        )

    if not is_ajax(request):
        return HttpResponseBadRequest("Unknown POST request")

    pending_api_read = json.loads(request.body.decode("utf-8"))

    if pending_api_read["task_id"]:
        # Task is pending, check state and return result if ready
        async_result = AsyncResult(pending_api_read["task_id"])
        if not settings.DEBUG:
            if async_result.ready():
                pending_api_read["ready"] = True
                pending_api_read["task_id"] = None
                if async_result.state == "SUCCESS":
                    exercise_data = async_result.get()
                    async_result.forget()
                    config_table = template_loader.get_template(
                        "review/configure_table.html"
                    )
                    exercise_data["config_type"] = pending_api_read["config_type"]
                    pending_api_read["resultHTML"] = config_table.render(
                        exercise_data, request
                    )
                else:
                    pending_api_read["resultHTML"] = ''
        else:
            # Debug mode, return result immediately
            print("DEBUG MODE")
            pending_api_read["ready"] = True
            exercise_data = pending_api_read['task_id']
            config_table = template_loader.get_template("review/configure_table.html")
            exercise_data["config_type"] = pending_api_read["config_type"]
            pending_api_read["resultHTML"] = config_table.render(exercise_data, request)
            pending_api_read["task_id"] = None
        return JsonResponse(pending_api_read)

    if pending_api_read["ready"]:
        # The client might be polling a few times even after it has received the results
        return JsonResponse(pending_api_read)

    # Put full read of provider API on task queue and store the task id for tracking
    has_radar_config = pending_api_read["config_type"] == "automatic"
    async_api_read = configured_function(p_config, "async_api_read")
    pending_api_read["task_id"] = async_api_read(request, course, has_radar_config)
    return JsonResponse(pending_api_read)


@access_resource
def exercise_settings(
    request, course_key=None, exercise_key=None, course=None, exercise=None
):
    p_config = provider_config(course.provider)
    context = {
        "hierarchy": (
            (settings.APP_NAME, reverse("index")),
            (course.name, reverse("course", kwargs={"course_key": course.key})),
            ("%s settings" % (exercise.name), None),
        ),
        "course": course,
        "exercise": exercise,
        "provider_reload": "full_reload" in p_config,
        "change_success": set(),
        "change_failure": {},
    }
    if request.method == "POST":
        if "save" in request.POST:
            form = ExerciseForm(request.POST)
            if form.is_valid():
                form.save(exercise)
                context["change_success"].add("save")
        elif "override_template" in request.POST:
            form_template = ExerciseTemplateForm(request.POST)
            if form_template.is_valid():
                form_template.save(exercise)
                context["change_success"].add("override_template")
        elif "clear_and_recompare" in request.POST:
            set_use_staff_submissions(request, exercise)
            configured_function(p_config, "recompare")(exercise, p_config)
            context["change_success"].add("clear_and_recompare")
        elif "provider_reload" in request.POST:
            set_use_staff_submissions(request, exercise)
            configured_function(p_config, "full_reload")(exercise, p_config)
            context["change_success"].add("provider_reload")
        elif "delete_exercise" in request.POST:
            form = DeleteExerciseFrom(request.POST)
            if form.is_valid() and form.cleaned_data["name"] == exercise.name:
                exercise.delete()
                return redirect("course", course_key=course.key)

            context["change_failure"]["delete_exercise"] = form.cleaned_data["name"]

    template_source = configured_function(p_config, 'get_exercise_template')(
        exercise, p_config
    )
    if exercise.template_tokens and not template_source:
        context["template_source_error"] = True
        context["template_tokens"] = exercise.template_tokens
        context["template_source"] = ''
    else:
        context["template_source"] = template_source
    context["form"] = ExerciseForm(
        {
            "name": exercise.name,
            "paused": exercise.paused,
            "tokenizer": exercise.tokenizer,
            "minimum_match_tokens": exercise.minimum_match_tokens,
        }
    )
    context["form_template"] = ExerciseTemplateForm(
        {
            "template": template_source,
        }
    )
    context["form_delete_exercise"] = DeleteExerciseFrom({"name": ''})
    context["use_staff_submissions"] = exercise.use_staff_submissions
    return render(request, "review/exercise_settings.html", context)

# Functions for toggling the use of staff submissions
def set_use_staff_submissions(request, exercise):
    if "use_staff_submissions" in request.POST:
        exercise.use_staff_submissions = True
    else:
        exercise.use_staff_submissions = False
    exercise.save()

def download_file(output_dir, submission, local_course):
    filename = "student" + submission.student.key
    p_config = provider_config(local_course.provider)
    get_submission_text = configured_function(p_config, "get_submission_text")

    with open(os.path.join(output_dir, filename + "|" + str( "Points: " + str(submission.grade))), 'w') as f:
        submission_text = get_submission_text(submission, p_config)
        print("Writing something with length: ", len(submission_text))
        f.write(submission_text)

def download_files(output_dir, local_exercise, local_course, submissions):
    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = [executor.submit(download_file, output_dir, sub, local_course) for
                   sub in submissions]
        concurrent.futures.wait(futures)

def zip_files(directory, output_dir):
    # Get the base filename from the directory path
    base = os.path.basename(os.path.normpath(directory))

    # Create a zip file with the same name as the directory in the specified location
    output_zip_file = os.path.join(output_dir, f'{base}.zip')
    with zipfile.ZipFile(output_zip_file, 'w') as zip_handle:
        for foldername, subfolders, filenames in os.walk(directory):  # pylint: disable=unused-variable
            for filename in filenames:
                # Create complete filepath of file in directory
                file_path = os.path.join(foldername, filename)

                # Add file to zip
                zip_handle.write(file_path, arcname=filename)


def write_metadata_for_dolos(exercise_directory, local_exercise, submissions) -> None:
    # Write metadata to CSV file
    with open(exercise_directory + '/info.csv', 'w', newline='') as csvfile:
        writer = csv.writer(csvfile)

        writer.writerow(['filename', 'label', 'created_at'])

        for submission in submissions:
            filename = "student" + submission.student.key + "|" + "Points: " + str(submission.grade)
            created_at = submission.provider_submission_time

            if isinstance(created_at, datetime.datetime):
                created_at = created_at.strftime('%Y-%m-%d %H:%M:%S %z')

            writer.writerow([filename, submission.student.key, created_at])


def go_to_dolos_view(request, course_key=None, exercise_key=None) -> HttpResponse:
    course = Course.objects.get(key=course_key)
    exercise = course.get_exercise(exercise_key)
    if exercise.dolos_report_key != "":
        exercise.dolos_report_status = exercise.dolos_report_status + "\n Could not find report"
        exercise.save()

        return redirect(f"{DOLOS_PROXY_WEB_URL}/#/share/{exercise.dolos_report_id}")
    return HttpResponse("No report generated yet")


@access_resource
def generate_dolos_view(
    request, course_key=None, exercise_key=None, course=None, exercise=None, best_submissions=False, flagged=False
                        ) -> HttpResponse:
    """
    Create a Dolos report of this exercise and redirect to the report visualization
    """
    # Generate new report if timestamp is over 1 hour old or if one does not exist. Disabled for testing
    temp_submissions_dir = os.path.join(os.path.abspath(os.getcwd()), "temp_submission_files")
    if not os.path.exists(temp_submissions_dir):
        os.mkdir(temp_submissions_dir)

    new_submissions_dir = os.path.join(temp_submissions_dir, exercise.key)
    if not os.path.exists(new_submissions_dir):
        os.mkdir(new_submissions_dir)

    submissions = (exercise.valid_submissions | exercise.invalid_submissions)
    if best_submissions == 'true':
        submissions = exercise.best_submissions
    elif flagged == 'true':
        submissions = exercise.flagged_submissions

    # Remove staff submissions
    if exercise.use_staff_submissions is False:
        submissions = submissions.exclude(student__is_staff=True)

    print("Submissions", submissions)

    download_files(new_submissions_dir, exercise, course, submissions.distinct())
    write_metadata_for_dolos(new_submissions_dir, exercise, submissions.distinct())
    zip_files(new_submissions_dir, temp_submissions_dir)

    timestamp = time.time()
    date_and_time = datetime.datetime.fromtimestamp(timestamp, pytz.timezone('Europe/Helsinki'))
    time_string = date_and_time.strftime('Day: %Y-%m-%d - Time: %H.%M.%S')

    programming_language = dolos_language(exercise.tokenizer)

    response = requests.post(
        DOLOS_API_SERVER_URL + '/reports',
        files={'dataset[zipfile]': open(temp_submissions_dir + "/" + exercise.key + ".zip", 'rb')},
        data={'dataset[name]': exercise.name + " | " + time_string,
            'dataset[programming_language]': programming_language},
    )
    # Remove the files in the folder new_submissions_dir
    for file in os.listdir(new_submissions_dir):
        os.remove(os.path.join(new_submissions_dir, file))
    os.remove(temp_submissions_dir + "/" + exercise.key + ".zip")

    try:
        json = response.json()
    except ValueError:
        print("Response is not in JSON format")

    exercise = course.get_exercise(exercise_key)
    exercise.dolos_report_status = "SENT"
    exercise.dolos_report_id = json['id']
    exercise.dolos_report_timestamp = time_string
    exercise.dolos_report_raw_timestamp = timestamp
    exercise.dolos_report_generated = True
    exercise.dolos_report_key = json['html_url']
    exercise.save()

    return go_to_dolos_view(request, course_key, exercise_key)

@method_decorator(csrf_exempt, name='dispatch')
class dolos_proxy_api_view(View):
    # Proxy the request to the Dolos API
    @method_decorator(login_required)
    def dispatch(self, request, *args, **kwargs) -> HttpResponse:
        path = kwargs.get('path', '')
        # Rewrite the URL: prepend the path with the upstream URL
        true_url = urljoin(DOLOS_API_SERVER_URL, path)

        # Prepare the headers
        headers = {key: value for (key, value) in request.META.items() if key.startswith('HTTP_')}
        headers.update({
            'content-type': "*/*",
            'content-length': str(len(request.body)),
        })

        # Prepare the files
        files = list(request.FILES.items())

        # If the path starts with 'static', download and save the file
        if path.startswith('static') or path.startswith('assets') or path.startswith('api/assets'):
            local_file_path = settings.BASE_DIR + "/dolos-api-proxy/" + path

            # Ensure the directory exists
            if not os.path.exists(os.path.dirname(local_file_path)):
                os.makedirs(os.path.dirname(local_file_path), exist_ok=True)

            with requests.get(true_url, stream=True) as r:
                r.raise_for_status()
                with open(local_file_path, 'wb') as f:
                    f.write(r.content)

            if not path.endswith('.ttf') and not path.endswith('.woff2'):
                with open(local_file_path, 'r') as file:
                    filedata = file.read()
                    filedata = filedata.replace(DOLOS_API_SERVER_URL, DOLOS_PROXY_API_URL)
                with open(local_file_path, 'w') as file:
                    file.write(filedata)

            # Determine the file's MIME type
            content_type, _ = mimetypes.guess_type(local_file_path)

            # Send the file as a response
            return FileResponse(open(local_file_path, 'rb'), content_type=content_type)

        # Send the proxied request to the upstream service
        response = requests.request(
            method=request.method,
            url=true_url,
            data=request.body,
            headers=headers,
            files=files,
            cookies=request.COOKIES,
            allow_redirects=True,
        )

        response_content = response.content.replace(DOLOS_API_SERVER_URL.encode(), DOLOS_PROXY_API_URL.encode())

        # Create a Django HttpResponse from the upstream response
        proxy_response = HttpResponse(
            content=response_content,
            status=response.status_code,
        )

        # Add the Access-Control-Allow-Origin header
        proxy_response['Access-Control-Allow-Origin'] = '*'

        # Set the Content-Type header based on the file type
        if request.path.endswith('.css'):
            proxy_response['Content-Type'] = 'text/css'
        elif request.path.endswith('.js'):
            proxy_response['Content-Type'] = 'application/javascript'
        elif request.path.endswith('.ttf'):
            proxy_response['Content-Type'] = 'font/ttf'
        elif request.path.endswith('.woff'):
            proxy_response['Content-Type'] = 'font/woff'
        elif request.path.endswith('.woff2'):
            proxy_response['Content-Type'] = 'font/woff2'
        elif request.path.endswith('.csv'):
            proxy_response['Content-Type'] = 'text/csv'

        return proxy_response

@method_decorator(csrf_exempt, name='dispatch')
@method_decorator(xframe_options_sameorigin, name='dispatch')
class dolos_proxy_view(View):
    @method_decorator(login_required)
    def dispatch(self, request, *args, **kwargs) -> HttpResponse:
        path = kwargs.get('path', '')
        # Rewrite the URL: prepend the path with the upstream URL
        true_url = urljoin(DOLOS_WEB_SERVER_URL, path)

        # Prepare the headers
        headers = {key: value for (key, value) in request.META.items() if key.startswith('HTTP_')}
        headers.update({
            'content-type': "*/*",
            'content-length': str(len(request.body)),
        })

        # Prepare the files
        files = list(request.FILES.items())

        # If the path starts with 'static', download and save the file
        if path.startswith('static') or path.startswith('assets') or path.startswith('api/assets'):
            local_file_path = settings.BASE_DIR + "/dolos-proxy/" + path

            # Ensure the directory exists
            if not os.path.exists(os.path.dirname(local_file_path)):
                os.makedirs(os.path.dirname(local_file_path), exist_ok=True)

            with requests.get(true_url, stream=True) as r:
                r.raise_for_status()
                with open(local_file_path, 'wb') as f:
                    f.write(r.content)

            # If a file contains localhost:3000, replace it with localhost:8000/dolos-api-proxy.
            # But only if the file is not a tff or woff2 file
            if not path.endswith('.ttf') and not path.endswith('.woff2'):
                with open(local_file_path, 'r') as file:
                    filedata = file.read()
                    filedata = filedata.replace(DOLOS_API_SERVER_URL, DOLOS_PROXY_API_URL)
                with open(local_file_path, 'w') as file:
                    file.write(filedata)

            # Determine the file's MIME type
            content_type, _ = mimetypes.guess_type(local_file_path)

            # Send the file as a response
            return FileResponse(open(local_file_path, 'rb'), content_type=content_type)

        # Send the proxied request to the upstream service
        response = requests.request(
            method=request.method,
            url=true_url,
            data=request.body,
            headers=headers,
            files=files,
            cookies=request.COOKIES,
            allow_redirects=True,
        )


        # Create a Django HttpResponse from the upstream response
        # Replace the API server URL with the proxy URL so the frontend can reach the API
        response_content = response.content.replace(
            DOLOS_API_SERVER_URL.encode(), DOLOS_PROXY_API_URL.encode()
        )

        proxy_response = HttpResponse(
            content=response_content,
            status=response.status_code,
        )

        # Add the Access-Control-Allow-Origin header
        proxy_response['Access-Control-Allow-Origin'] = '*'

        # Set the Content-Type header based on the file type
        if path.endswith('.css'):
            proxy_response['Content-Type'] = 'text/css'
        elif path.endswith('.js'):
            proxy_response['Content-Type'] = 'application/javascript'
        elif path.endswith('.ttf'):
            proxy_response['Content-Type'] = 'font/ttf'
        elif path.endswith('.woff'):
            proxy_response['Content-Type'] = 'font/woff'
        elif path.endswith('.woff2'):
            proxy_response['Content-Type'] = 'font/woff2'
        elif path.endswith('.csv'):
            proxy_response['Content-Type'] = 'text/csv'
        return proxy_response

# ---------------------------------------------------------------------------
# Aggregate Dolos reports: per-course (students across exercises) and
# cross-course (exercises across courses). See review/dolos_reports.py for the
# dataset/metadata format that lets Dolos understand this structure.
# ponytail: info.csv metadata drives Dolos visualisation only; Dolos still
# cross-compares every file, so aggregating exercises adds noise you scope
# visually by label (exercise/course) and by the course/exercise ZIP path.
# ---------------------------------------------------------------------------


def _make_get_text():
    """Return get_text(submission) -> source, caching provider config per provider."""
    cache = {}

    def get_text(submission):
        provider = submission.exercise.course.provider
        resolved = cache.get(provider)
        if resolved is None:
            p_config = provider_config(provider)
            resolved = (configured_function(p_config, "get_submission_text"), p_config)
            cache[provider] = resolved
        get_submission_text, p_config = resolved
        return get_submission_text(submission, p_config) or ""

    return get_text


def _exercise_submissions(exercise, include_all=False):
    """Submissions to feed Dolos: best-per-student by default, or every valid one
    (several per student) when include_all. Staff excluded unless opted in."""
    subs = exercise.valid_submissions if include_all else exercise.best_submissions
    if not exercise.use_staff_submissions:
        subs = subs.exclude(student__is_staff=True)
    return subs


def _course_submissions(course, include_all=False):
    for exercise in course.exercises.all():
        yield from _exercise_submissions(exercise, include_all)


def _generate_dolos_report(submissions, name, programming_language, label_fn):
    """Build + upload a Dolos dataset. Returns the report id, or None if empty."""
    submissions = list(submissions)
    # Dolos needs at least two files to compare (dolos.ts throws otherwise).
    if len(submissions) < 2:
        return None
    work_dir = tempfile.mkdtemp(prefix="dolos_")
    zip_fd, zip_path = tempfile.mkstemp(suffix=".zip")
    os.close(zip_fd)
    try:
        write_dataset(work_dir, submissions, label_fn, _make_get_text())
        zip_dataset(work_dir, zip_path)
        with open(zip_path, "rb") as zip_handle:
            response = requests.post(
                DOLOS_API_SERVER_URL + "/reports",
                files={"dataset[zipfile]": zip_handle},
                data={
                    "dataset[name]": name,
                    "dataset[programming_language]": programming_language,
                },
            )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        os.remove(zip_path)
    return response.json().get("id")


def _dolos_report_name(prefix):
    stamp = datetime.datetime.now(pytz.timezone("Europe/Helsinki"))
    return "%s | %s" % (prefix, stamp.strftime("Day: %Y-%m-%d - Time: %H.%M.%S"))


@access_resource
def generate_course_dolos_view(request, course_key=None, course=None) -> HttpResponse:
    """Dolos report over every exercise in a course (students across exercises)."""
    report_id = _generate_dolos_report(
        _course_submissions(course),
        _dolos_report_name(course.name),
        dolos_language(course.tokenizer),
        label_fn=lambda submission: submission.exercise.name,
    )
    if report_id is None:
        return HttpResponse("Need at least two submissions to analyse in this course")
    return redirect("%s/#/share/%s" % (DOLOS_PROXY_WEB_URL, report_id))


@login_required
def generate_cross_course_dolos_view(request) -> HttpResponse:
    """Dolos report over every accessible course (exercises across courses)."""
    submissions = []
    for course in Course.objects.get_available_courses(request.user):
        submissions.extend(_course_submissions(course))
    report_id = _generate_dolos_report(
        submissions,
        _dolos_report_name("All courses"),
        # ponytail: courses may mix languages -> "char" (character-based) always
        # works; split by language and pass a specific Dolos language if noisy.
        "char",
        label_fn=lambda submission: submission.exercise.course.name,
    )
    if report_id is None:
        return HttpResponse("Need at least two submissions to analyse")
    return redirect("%s/#/share/%s" % (DOLOS_PROXY_WEB_URL, report_id))


def _cached_report_id(cache_key, generate):
    # ponytail: best-effort 1h cache so switching hub scopes reuses reports; a
    # missing/broken memcached just regenerates, and staleness clears on TTL.
    try:
        report_id = cache.get(cache_key)
    except Exception:
        report_id = None
    if report_id:
        return report_id
    report_id = generate()
    if report_id:
        try:
            cache.set(cache_key, report_id, 60 * 60)
        except Exception:
            pass
    return report_id


def _too_few_message(selected_count, include_all, total, students, staff_excluded, scope):
    """Explain why Dolos has fewer than two files to compare."""
    mode = "all submissions" if include_all else (
        "best submission per student" + (" per exercise" if scope == "course" else "")
    )
    msg = (
        "Dolos needs at least 2 files to compare, but only %d matched the current "
        "filter (%s). This %s has %d submission(s) from %d student(s)%s."
        % (selected_count, mode, scope, total, students,
           " with staff excluded" if staff_excluded else "")
    )
    if not include_all and total >= 2:
        msg += " Switch to \u201cAll submissions\u201d above to include every submission."
    return msg


@access_resource
def dolos_hub(request, course_key=None, exercise_key=None, course=None, exercise=None) -> HttpResponse:
    """
    Dolos navigation hub: a Radar nav bar (whole-course view + every exercise)
    around the Dolos report embedded in an iframe. Defaults to the whole-course
    report; a single exercise when exercise_key is given. ``?all=1`` includes
    every submission (several per student) instead of only the best per student.
    Report ids are cached so switching scopes reuses reports.
    """
    # Legacy Radar has no hub: send these URLs to the classic views so the mode
    # toggle takes effect even from inside a Dolos report.
    if request.session.get("legacy_radar", True):
        if exercise is not None:
            return redirect("exercise", course_key=course.key, exercise_key=exercise.key)
        return redirect("course", course_key=course.key)

    include_all = request.GET.get("all") == "1"
    mode = "all" if include_all else "best"

    if exercise is not None:
        selected = list(_exercise_submissions(exercise, include_all))

        def label_fn(sub):
            return sub.student.display_name

        name, language = (
            _dolos_report_name(exercise.name),
            dolos_language(exercise.tokenizer),
        )
        cache_key = "dolos_report:ex:%d:%s" % (exercise.id, mode)
        total = exercise.submissions.count()
        students = exercise.submissions.values("student").distinct().count()
        scope, staff_excluded = "exercise", not exercise.use_staff_submissions
    else:
        selected = list(_course_submissions(course, include_all))

        def label_fn(sub):
            return sub.exercise.name

        name, language = (
            _dolos_report_name(course.name),
            dolos_language(course.tokenizer),
        )
        cache_key = "dolos_report:course:%d:%s" % (course.id, mode)
        total = course.submissions.count()
        students = course.submissions.values("student").distinct().count()
        scope, staff_excluded = "course", True

    if len(selected) < 2:
        report_id = None
        message = _too_few_message(len(selected), include_all, total, students, staff_excluded, scope)
    else:
        report_id = _cached_report_id(
            cache_key, lambda: _generate_dolos_report(selected, name, language, label_fn)
        )
        message = None if report_id else (
            "Dolos accepted %d files but returned no report \u2014 is the Dolos API "
            "(%s) running?" % (len(selected), DOLOS_API_SERVER_URL)
        )

    return render(
        request,
        "review/dolos_hub.html",
        {
            "hierarchy": (
                (settings.APP_NAME, reverse("index")),
                (course.name, reverse("course", kwargs={"course_key": course.key})),
                ("Dolos" if exercise is None else exercise.name, None),
            ),
            "course": course,
            "exercises": course.exercises.all(),
            "current_exercise": exercise,
            "include_all": include_all,
            "message": message,
            "report_url": (
                "%s/#/share/%s" % (DOLOS_PROXY_WEB_URL, report_id) if report_id else None
            ),
        },
    )


# ---------------------------------------------------------------------------
# Legacy Radar: the original comparison / graph / cluster views. Reachable only
# in Legacy Radar mode (New Radar routes to the Dolos hub instead).
# ---------------------------------------------------------------------------


# Render a single comparison between two submissions
@access_resource
def comparison(
    request: WSGIRequest,
    course_key: str | None = None,
    exercise_key: str | None = None,
    ak: str | None = None,
    bk: str | None = None,
    ck: str | None = None,
    course: Course | None = None,
    exercise: Exercise | None = None,
) -> HttpResponse:

    comparison = get_object_or_404(
        Comparison,
        submission_a__exercise=exercise,
        pk=ck,
        submission_a__student__key=ak,
        submission_b__student__key=bk,
    )
    if request.method == "POST":
        result = "review" in request.POST and comparison.update_review(
            request.POST["review"]
        )
        if is_ajax(request):
            return JsonResponse({"success": result})

    reverse_flag = False
    a = comparison.submission_a
    b = comparison.submission_b
    if "reverse" in request.GET:
        reverse_flag = True
        a = comparison.submission_b
        b = comparison.submission_a

    p_config = provider_config(course.provider)
    get_submission_text = configured_function(p_config, "get_submission_text")

    # Get the top comparisons for the exercise
    top_comparisons = json.loads(exercise.best_comparisons or '[]')

    # Create a regex to find the current comparison in the top comparisons
    r = re.compile(r'../(.+)-(.+)/' + re.escape(str(comparison.id)))

    try:
        # Find the index of the current comparison in the top comparisons
        index = [i for i, c in enumerate(top_comparisons) if re.search(r, c)][0]

        # Get the next and previous comparisons based on the index
        if index + 1 < len(top_comparisons):
            next_comparison = top_comparisons[index + 1]
        else:
            next_comparison = -1

        if index - 1 >= 0:
            previous_comparison = top_comparisons[index - 1]
        else:
            previous_comparison = -1
    except IndexError:
        index = -1
        next_comparison = -1
        previous_comparison = -1

    context = {
        "hierarchy": (
            (settings.APP_NAME, reverse("index")),
            (course.name, reverse("course", kwargs={"course_key": course.key})),
            (
                exercise.name,
                reverse(
                    "exercise",
                    kwargs={"course_key": course.key, "exercise_key": exercise.key},
                ),
            ),
            ("%s → %s" % (a.student.key, b.student.key), None),
        ),
        "course": course,
        "exercise": exercise,
        "comparisons": exercise.comparisons_for_student(a.student),
        "comparison": comparison,
        "reverse": reverse_flag,
        "a": a,
        "b": b,
        "source_a": get_submission_text(a, p_config),
        "source_b": get_submission_text(b, p_config),
        "next_comparison": next_comparison,
        "previous_comparison": previous_comparison,
        "index": index,
        "top_comparisons": len(top_comparisons),
    }

    return render(
        request,
        "review/comparison.html",
        context,
    )


@access_resource
def marked_submissions(request, course_key=None, course=None):
    comparisons = (
        Comparison.objects.filter(submission_a__exercise__course=course, review__gte=5)
        .order_by("submission_a__created")
        .select_related(
            "submission_a",
            "submission_b",
            "submission_a__exercise",
            "submission_a__student",
            "submission_b__student",
        )
    )
    suspects = {}
    for c in comparisons:
        for s in (c.submission_a.student, c.submission_b.student):
            if s.id not in suspects:
                suspects[s.id] = {'key': s.key, 'sum': 0, 'comparisons': []}
            suspects[s.id]['sum'] += c.review
            suspects[s.id]['comparisons'].append(c)
    return render(
        request,
        "review/marked.html",
        {
            "hierarchy": (
                (settings.APP_NAME, reverse("index")),
                (course.name, reverse("course", kwargs={"course_key": course.key})),
                ("Marked submissions", None),
            ),
            "course": course,
            "suspects": sorted(suspects.values(), reverse=True, key=lambda e: e['sum']),
        },
    )


@access_resource
def graph_ui(request, course, course_key):
    """Course graph UI without the graph data."""
    context = {
        "hierarchy": (
            (settings.APP_NAME, reverse("index")),
            (course.name, reverse("course", kwargs={"course_key": course.key})),
            ("Graph", None),
        ),
        "course": course,
        "minimum_similarity_threshold": settings.MATCH_STORE_MIN_SIMILARITY,
        "number_of_exercises": course.exercises.count(),
    }
    return render(request, "review/graph.html", context)


# This view is used to build the graph data and clusters for the course.
@access_resource
def build_graph(request: WSGIRequest, course: Course | None = None, course_key: str | None = None) -> JsonResponse:
    # If the request is not a POST request or not an AJAX request, return a 400 error
    if request.method != "POST" or not is_ajax(request):
        return HttpResponseBadRequest()

    # Load the task state from the request body
    task_state = json.loads(request.body.decode("utf-8"))

    # Check if the task state is pending
    if task_state["task_id"]:
        task_state = handle_async_task(task_state, course.key)

    # If the task is ready
    elif not task_state["ready"]:
        # Check if the graph data is already cached in the database
        graph_data = json.loads(course.similarity_graph_json or '{}')

        # Get the provider configuration
        p_config = provider_config(course.provider)

        # Get the parameters for the graph
        min_similarity, min_matches, use_unique_ex, origin = (
            task_state["min_similarity"],
            task_state["min_matches"],
            task_state["unique_exercises"],
            task_state["origin"],
        )

        # Check if the graph data is already cached and matches the parameters
        if (
            graph_data
            and graph_data["min_similarity"] == min_similarity
            and graph_data["min_matches"] == min_matches
            and graph_data["unique_exercises"] == use_unique_ex
        ):
            # Check if the clusters are already cached in the database
            clusters = json.loads(course.clusters_json or '{}')

            # Check if the clusters match the parameters
            if (
            clusters
            and clusters["min_similarity"] == min_similarity
            and clusters["min_matches"] == min_matches
            and clusters["unique_exercises"] == use_unique_ex
            and clusters["origin"] == origin
            ):
                # Clusters and graph was already cached
                task_state["graph_data"] = graph_data
                task_state["clusters"] = clusters["clusters"]
                task_state["ready"] = True

            else:
                # Graph was already cached, but clusters not
                task_state["graph_data"] = graph_data

                # Build clusters
                if p_config.get("async_graph", True) or CELERY_DEBUG:
                    async_task = build_clusters_for(task_state, course.key, delay=True)
                    task_state["task_id"] = [None, async_task.id]
                else:
                    task_state["clusters"] = build_clusters_for(task_state, course.key)
                    task_state["ready"] = True

        else:
            # No graph cached, build graph and clusters
            if p_config.get("async_graph", True) or CELERY_DEBUG:
                async_task = graph.generate_match_graph.delay(
                    course.key, float(min_similarity), int(min_matches), use_unique_ex
                )
                task_state["task_id"] = [async_task.id, None]
            else:
                task_state["graph_data"] = graph.generate_match_graph(
                    course.key, float(min_similarity), int(min_matches), use_unique_ex
                )
                task_state["clusters"] = build_clusters_for(task_state, course.key)
                task_state["ready"] = True

    return JsonResponse(task_state)


@access_resource
def invalidate_graph_cache(request, course, course_key):
    course.similarity_graph_json = ''
    course.clusters_json = ''
    course.save()
    return HttpResponse("Graph cache invalidated")


@access_resource
def students_view(request: WSGIRequest, course: Course | None = None, course_key: str | None = None) -> HttpResponse:
    """
    Students view listing students and average/max similarity scores of their submissions
    """

    # Get all submissions for the course
    submissions = (
        Submission.objects.filter(exercise__course=course)
        .values('student__key', 'student__is_staff', 'exercise__name')
        .annotate(
            avg_similarity=Avg('max_similarity'),
        )
    )

    # Exercise names as a list
    exercise_names = course.exercises.all().values_list('name', flat=True)

    # Exercise ids as a list
    exercise_ids = course.exercises.all().values_list('key', flat=True)

    # Student keys
    student_info = submissions.values_list('student__key', 'student__is_staff').distinct()

    students = []

    # Loop through students and their submissions
    for student in student_info:
        # Get all submissions and their similarity for the student
        exercise_similarities = list(
            submissions.filter(student__key=student[0])
            .values_list('exercise__name', 'avg_similarity')
        )

        similarities = list(map(lambda x: x[1], exercise_similarities))

        # Append the student to the list of students
        students.append(
            {
                'key': student[0],
                'is_staff': student[1],
                'exercises': exercise_similarities,
                'avg_similarity': sum(similarities) / len(similarities) if len(similarities) > 0 else "",
            }
        )

        # Check if their submissions are missing for some exercises
        if len(exercise_similarities) == len(exercise_names):
            students[-1]['exercises'] = sorted(students[-1]['exercises'], key=lambda x: x[0])
            continue

        # Add missing exercises
        for exercise in exercise_names:
            if not any(d[0] == exercise for d in students[-1]['exercises']):
                students[-1]['exercises'].append((exercise, ""))

                if len(students[-1]['exercises']) == len(exercise_names):
                    students[-1]['exercises'] = sorted(students[-1]['exercises'], key=lambda x: x[0])
                    break

    context = {
        "hierarchy": (
            (settings.APP_NAME, reverse("index")),
            (course.name, reverse("course", kwargs={"course_key": course.key})),
            ("Students", None),
        ),
        "course": course,
        "exercises": dict(zip(exercise_ids, exercise_names)),
        "students": students,
    }

    return render(request, "review/students_view.html", context)


@access_resource
def student_view(request, course=None, course_key=None, student=None, student_key=None):

    comparisons = (
        Comparison.objects.filter(submission_a__exercise__course=course)
        .filter(similarity__gt=0.75)
        .select_related(
            "submission_a",
            "submission_b",
            "submission_a__exercise",
            "submission_b__exercise",
            "submission_a__student",
            "submission_b__student",
        )
        .filter(
            Q(submission_a__student__key=student_key)
        )
        .exclude(submission_b__isnull=True)
    )

    context = {
        "hierarchy": (
            (settings.APP_NAME, reverse("index")),
            (course.name, reverse("course", kwargs={"course_key": course.key})),
            ("Students", reverse("students_view", kwargs={"course_key": course.key})),
            (student_key, None),
        ),
        "course": course,
        "exercises": course.exercises.all(),
        "student": student_key,
        "comparisons": comparisons,
        "row": range(5),
    }

    return render(request, "review/student_view.html", context)


@access_resource
def pair_view(
    request, course=None, course_key=None, a=None, a_key=None, b=None, b_key=None
):

    authors = {a_key, b_key}
    comparisons = (
        Comparison.objects.filter(submission_a__exercise__course=course)
        .filter(similarity__gt=0)
        .select_related(
            "submission_a",
            "submission_b",
            "submission_a__exercise",
            "submission_b__exercise",
            "submission_a__student",
            "submission_b__student",
        )
        .filter(
            Q(submission_a__student__key__in=authors)
            & Q(submission_b__student__key__in=authors)
        )
    )

    context = {
        "hierarchy": (
            (settings.APP_NAME, reverse("index")),
            (course.name, reverse("course", kwargs={"course_key": course.key})),
            ("%s → %s" % (a_key, b_key), None),
        ),
        "course": course,
        "exercises": course.exercises.all(),
        "a": a_key,
        "b": b_key,
        "comparisons": comparisons,
    }

    return render(request, "review/pair_view.html", context)


@access_resource
def pair_view_summary(
    request, course=None, course_key=None, a=None, a_key=None, b=None, b_key=None
):

    authors = {a_key, b_key}

    a = Student.objects.get(key=a_key, course=course)
    b = Student.objects.get(key=b_key, course=course)

    # Get comparisons of authors marked as plagiarized
    comparisons = (
        Comparison.objects.filter(submission_a__exercise__course=course)
        .filter(similarity__gt=0)
        .select_related(
            "submission_a",
            "submission_b",
            "submission_a__exercise",
            "submission_b__exercise",
            "submission_a__student",
            "submission_b__student",
        )
        .filter(
            Q(submission_a__student__key__in=authors)
            & Q(submission_b__student__key__in=authors)
        )
        .filter(review=settings.REVIEW_CHOICES[4][0])
    )

    p_config = provider_config(course.provider)
    get_submission_text = configured_function(p_config, "get_submission_text")
    sources = []

    # Loop through comparisons and add to sources
    for n in comparisons:
        reverse_flag = False
        student_a = n.submission_a.student.key
        student_b = n.submission_b.student.key
        text_a = n.submission_a
        text_b = n.submission_b
        submission_text_a = get_submission_text(text_a, p_config)
        submission_text_b = get_submission_text(text_b, p_config)
        matches = n.matches_json
        template_comparisons_a = text_a.template_comparison.matches_json
        template_comparisons_b = text_b.template_comparison.matches_json
        indexes_a = text_a.indexes_json
        indexes_b = text_b.indexes_json
        exercise = n.submission_a.exercise.name

        if "reverse" in request.GET:
            reverse_flag = True
            text_a = n.submission_b
            text_b = n.submission_a
        sources.append(
            {
                "text_a": submission_text_a,
                "text_b": submission_text_b,
                "matches": matches,
                "templates_a": template_comparisons_a,
                "templates_b": template_comparisons_b,
                "indexes_a": indexes_a,
                "indexes_b": indexes_b,
                "reverse_flag": reverse_flag,
                "student_a": student_a,
                "student_b": student_b,
                "exercise": exercise,
            }
        )

    context = {
        "hierarchy": (
            (settings.APP_NAME, reverse("index")),
            (course.name, reverse("course", kwargs={"course_key": course.key})),
            (
                "%s → %s" % (a_key, b_key),
                reverse(
                    "pair_view",
                    kwargs={"course_key": course_key, "a_key": a_key, "b_key": b_key},
                ),
            ),
            ("Summary", None),
        ),
        "course": course,
        "a": a_key,
        "b": b_key,
        "a_object": a,
        "b_object": b,
        "sources": sources,
        "time": now,
    }

    return render(request, "review/pair_view_summary.html", context)


@access_resource
def flagged_pairs(request, course=None, course_key=None):

    # Get comparisons of students with flagged plagiates
    comparisons = (
        Comparison.objects.filter(submission_a__exercise__course=course)
        .select_related(
            "submission_a",
            "submission_b",
            "submission_a__exercise",
            "submission_a__student",
            "submission_b__student",
        )
        .filter(similarity__gt=0)
        .filter(review=settings.REVIEW_CHOICES[4][0])
    )

    context = {
        "hierarchy": (
            (settings.APP_NAME, reverse("index")),
            (course.name, reverse("course", kwargs={"course_key": course.key})),
            ("Flagged pairs", None),
        ),
        "course": course,
        "comparisons": comparisons,
    }

    return render(request, "review/flagged_pairs.html", context)


# Render the clusters view
@access_resource
def clusters_view(request: WSGIRequest, course: Course | None = None, course_key: str | None = None) -> HttpResponse:

    context = {
        "hierarchy": (
            (settings.APP_NAME, reverse("index")),
            (course.name, reverse("course", kwargs={"course_key": course.key})),
            ("Clusters", None),
        ),
        "minimum_similarity_threshold": settings.MATCH_STORE_MIN_SIMILARITY,
        "number_of_exercises": course.exercises.count(),
    }

    return render(request, "review/clusters_view.html", context)


# Render the cluster view
@access_resource
def cluster_view(
    request: WSGIRequest,
    cluster_key: str,
    course: Course | None = None,
    course_key: str | None = None
    ) -> HttpResponse:

    # Get the cluster data from the course object
    cluster_data = json.loads(course.clusters_json or '{}')
    if not cluster_data:
        return HttpResponseBadRequest("No cluster data found")

    # Get the cluster data
    min_similarity = cluster_data["min_similarity"]
    min_matches = cluster_data["min_matches"]
    use_unique_ex = cluster_data["unique_exercises"]
    date_time = cluster_data["date_time"]
    cluster = cluster_data["clusters"][int(cluster_key) - 1]
    students = cluster["students"]

    # Get all student similarities for the course
    comparisons = (
        Comparison.objects.filter(submission_a__exercise__course=course)
        .filter(submission_a__student__key__in=students, submission_b__student__key__in=students)
        .annotate(
            student_a=F("submission_a__student__key"),
            student_b=F("submission_b__student__key"),
        )
        .values("student_a", "student_b")
        .annotate(
            avg_similarity=Avg("similarity"),
        )
    )

    # Get the max similarity for each student
    students_sorted = list(
        comparisons.values("student_b")
        .annotate(max_similarity=Avg("similarity"))
        .order_by("-max_similarity")
        .values_list("student_b", flat=True)
    )

    # Create a grid of student pairs and their average similarity
    grid = {}
    for comparison in comparisons:
        grid[comparison["student_a"] + '_' + comparison["student_b"]] = comparison["avg_similarity"]

    context = {
        "hierarchy": (
            (settings.APP_NAME, reverse("index")),
            (course.name, reverse("course", kwargs={"course_key": course.key})),
            ("Clusters", reverse("clusters_view", kwargs={"course_key": course.key})),
            (cluster_key, None),
        ),
        "course": course,
        "cluster_key": cluster_key,
        'min_similarity': min_similarity,
        'min_matches': min_matches,
        'use_unique_ex': use_unique_ex,
        'date_time': date_time,
        "students": students_sorted,
        "grid": grid,
    }

    return render(request, "review/cluster_view.html", context)
