# Radar

This document guides into different development tasks without installing and running the complete production system on a personal workstation.

The core of the system is written in [Python 3](https://www.python.org/) and installing that is the minimum requirement. Then, clone the project and get started!

## Set up python virtual environment

Following terminal commands create an environment and install required packages.

Linux:<br>
`source setup_for_linux.sh`<br>
OS X:<br>
`python3 -m virtualenv -p python3 py_venv`<br>
`source py_venv/bin/activate`<br>
`pip install -r requirements.txt`<br>
Windows:<br>
`C:\Python35\python -m venv py_venv`<br>
`py_venv\Scripts\activate.bat`<br>
`pip install -r requirements.txt`

When starting a new terminal for development, access the same virtual environment by entering the activate command alone (2nd line).

## Greedy String Tiling

The library has been copied into Radar `matcher/greedy_string_tiling` from the [original repo](https://github.com/apluslms/greedy-string-tiling). The library needs to be separately installed:

`cd matcher/greedy_string_tiling`<br>
`pip install .`

## Django

The repository contains a [Django web framework](https://www.djangoproject.com/) project where directory `radar` is the main module. Django offers a command line interface for controlling the system and this project further extends it with custom commands. Executing `python manage.py` lists all commands.

Useful maintenance actions:

1. `python manage.py migrate`<br>
   Creates/updates changed database models (default settings store data in filesystem `db.sqlite3`).
2. `python manage.py createsuperuser`<br>
   Creates a user that can login to the web UI.

Useful testing actions:

1. `python manage.py runserver`<br>
   Runs the web UI in http://localhost:8000/ as well as Django database admin in http://localhost:8000/admin/
2. `python manage.py loadsubmissions testcourse/exercise1 user1/sub1 user1/sub2 user2/sub1 ...`<br>
   Loads file submissions into analysis queue from the given directories (wildcards should work too, e.g. data/rainfall/9*/*). Each submission is a single directory - even if there is only one submitted file per submission.
3. `python manage.py matchsubmissions testcourse/exercise1`<br>
   Processes file submissions from the analysis queue. Beware, time requirement is exponential so
   better start with ten rather than hundred submissions. Once submissions have been inserted the
   button at top "Recompare all..." needs to be clicked to feed new submissions for matching.

Tip: create empty file `radar/local_settings.py` in order to get rid of extra warnings.

Note: The root folder contains the script "run_loadsubmission.sh" which correctly distributes submission files in a manner which Radar is happy with. To use the script for testing do the following:

1. Download submission zip file from A+ for example and unzip it. 
2. `./run_loadsubmissions.sh ${directory_with_submissions} {course}/{exercise} 1`<br>
   This goes through the directory, places each submission into it's own folder and creates a subfolder inside there to put the submission in. After this folder distribution is done, it runs the manage.py loadsubmissions command for each submission. The last variable is the delay, which determines how long we should wait between sending submissions to the service 
3. Wait until the script finishes
4. Run `python manage.py matchsubmissions {course}/exercise` and the submissions should be matched.

## Testing with A+ locally

To test the LTI integration and features between Radar and A+ locally, you first should follow the instructions in the root directory's README.rst on "Configuring with A+".

After this, ensure Radar is not running on the default localhost:8000. Use for example localhost:8009. This can be achieved by adding the desired port to the end of the runserver command `python manage.py runserver 8009`

Also ensure that DEBUG = True is set in the settings at root/radar/settings.py

To fetch submissions from A+ you need to also configure the APLUS_ROBOT_TOKEN variable in the settings.py file. For testing you may set it to the Api token for the root account. You should find it here: http://localhost:8000/accounts/accounts/

Now you should be able to login to Radar using LTI from A+ as well as import automatically or manually configured exercises from A+. Only thing not working is automatic fetching of submissions and automatic matching. To match submissions you will still have to run
`python manage.py matchsubmissions <course_id>/<exercise_id>`
