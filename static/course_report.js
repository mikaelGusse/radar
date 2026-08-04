/**
 * Whole-course Dolos report generation widget: "Generate Course Report"
 * button + progress panel, shared by dolos_hub.html and students_hub.html
 * (both include review/_course_report_panel.html, which loads this file).
 *
 * Previously this logic was duplicated inline in both templates and had
 * drifted out of sync: students_hub.html called a helper that assumed a
 * #hubCourseMeta element which only existed in dolos_hub.html, so its
 * "report ready" handler threw and silently got stuck. Every DOM lookup
 * here is therefore defensive (missing elements are simply skipped) so a
 * future template that omits a piece of the panel degrades instead of
 * breaking the whole handler.
 *
 * On success this stays on the current page and shows a clear success
 * message. Users explicitly asked to avoid redirect/reload jumps.
 */
(function () {
	var scriptEl = document.currentScript;
	if (!scriptEl) {
		return;
	}

	var generateUrl = scriptEl.getAttribute('data-generate-url');
	var checkUrlTemplate = scriptEl.getAttribute('data-check-url-template');
	var checkCurrentUrl = scriptEl.getAttribute('data-check-current-url');
	if (!generateUrl || !checkUrlTemplate || !checkCurrentUrl) {
		return;
	}

	var POLL_INTERVAL_MS = 3000;
	// Client-side safety net only; the server itself gives up on a stalled
	// task after 15 minutes (see COURSE_REPORT_STALE_SECONDS in views.py) and
	// reports "failed", so this just guards against an unexpected client-side
	// infinite loop rather than being the primary timeout.
	var MAX_POLL_ATTEMPTS = 500;

	var taskId = null;
	var pollHandle = null;
	var pollAttempts = 0;

	function el(id) {
		return document.getElementById(id);
	}

	function formatTimestamp(isoText) {
		if (!isoText) {
			return 'Generated recently';
		}
		var d = new Date(isoText);
		if (isNaN(d.getTime())) {
			return 'Generated recently';
		}
		return d.toLocaleString();
	}

	function setCourseMeta(completedAt) {
		var meta = el('hubCourseMeta');
		var stamp = el('hubCourseMetaTimestamp');
		if (!meta || !stamp) {
			return;
		}
		stamp.textContent = 'Last course-wide report: ' + formatTimestamp(completedAt);
		meta.classList.add('visible');
	}

	function setHint(message) {
		var hint = el('hubCourseProgressHint');
		if (!hint) {
			return;
		}
		if (message) {
			hint.textContent = message;
			hint.style.display = '';
		} else {
			hint.textContent = '';
			hint.style.display = 'none';
		}
	}

	function setProgressBar(data) {
		var bar = el('hubCourseProgressBar');
		if (!bar) {
			return;
		}
		if (data && data.exercises_total) {
			bar.max = data.exercises_total;
			bar.value = data.exercises_done || 0;
		} else {
			bar.removeAttribute('value');
			bar.removeAttribute('max');
		}
	}

	function phaseMessage(data) {
		if (data.current_exercise) {
			if (data.phase === 'processing') {
				var remaining = (data.exercises_remaining != null) ? ' ' + data.exercises_remaining + ' exercise(s) left.' : '';
				return 'Generating report for ' + data.current_exercise + '\u2026' + remaining;
			}
			if (data.phase === 'collecting') {
				return 'Collecting submissions for ' + data.current_exercise + '\u2026';
			}
			if (data.phase === 'fetching') {
				return 'Fetching submissions for ' + data.current_exercise + '\u2026';
			}
			if (data.phase === 'packaging') {
				return 'Packaging ' + data.current_exercise + ' submissions\u2026';
			}
			if (data.phase === 'uploading') {
				return 'Uploading ' + data.current_exercise + ' to Dolos\u2026';
			}
			if (data.phase === 'analyzing') {
				return 'Dolos is analyzing ' + data.current_exercise + '\u2026';
			}
		}
		if (data.phase === 'packaging') {
			return 'Packaging submissions for upload\u2026';
		}
		if (data.phase === 'uploading') {
			return 'Uploading dataset to Dolos\u2026';
		}
		if (data.phase === 'analyzing') {
			var seconds = data.analyzing_seconds ? ' (' + data.analyzing_seconds + 's)' : '';
			return 'Dolos is analyzing the dataset\u2026' + seconds + ' This can take a while for large courses.';
		}
		if (data.phase === 'collecting') {
			return 'Collecting submissions\u2026';
		}
		if (data.phase === 'complete') {
			return 'All exercises processed successfully!';
		}
		return 'Generating course-wide report\u2026 this can take a few minutes.';
	}

	function updateProgressText(data) {
		var text = el('hubCourseProgressText');
		if (text) {
			text.classList.remove('status-error');
			text.textContent = phaseMessage(data);
		}
		setProgressBar(data);
		setHint(data.hint || null);
	}

	function setBusy(busy) {
		var btn = el('generateCourseBtn');
		if (btn) {
			btn.disabled = busy;
		}
	}

	function stopPolling() {
		if (pollHandle) {
			window.clearInterval(pollHandle);
			pollHandle = null;
		}
	}

	function startPolling() {
		stopPolling();
		pollAttempts = 0;
		pollHandle = window.setInterval(checkCourseReportStatus, POLL_INTERVAL_MS);
	}

	function showFailure(message) {
		var progress = el('hubCourseProgress');
		var text = el('hubCourseProgressText');
		if (progress) {
			progress.classList.add('active');
		}
		if (text) {
			text.classList.add('status-error');
			text.textContent = 'Report generation failed: ' + (message || 'Unknown error');
		}
		setHint(null);
		setProgressBar(null);
		setBusy(false);
		var btn = el('generateCourseBtn');
		if (btn) {
			btn.textContent = 'Generate Course Report';
		}
		stopPolling();
	}

	function escapeHtml(text) {
		return String(text || '')
			.replace(/&/g, '&amp;')
			.replace(/</g, '&lt;')
			.replace(/>/g, '&gt;')
			.replace(/"/g, '&quot;')
			.replace(/'/g, '&#39;');
	}

	function renderReadyReports(data) {
		if (!data) {
			return;
		}

		var urls = [];
		if (Array.isArray(data.report_urls) && data.report_urls.length > 0) {
			urls = data.report_urls;
		} else if (data.report_url) {
			urls = [data.report_url];
		}

		if (!urls.length) {
			return;
		}

		var container = el('hubCourseReportResults');
		if (!container) {
			var progress = el('hubCourseProgress');
			if (!progress || !progress.parentNode) {
				return;
			}
			container = document.createElement('div');
			container.id = 'hubCourseReportResults';
			progress.parentNode.insertBefore(container, progress.nextSibling);
		}

		var html = [];
		html.push('<div class="hub-multi-report">');
		html.push('<p><strong>Course-wide report created:</strong> ' + urls.length + ' exercise report(s) generated.</p>');
		html.push('<p>Open the Students view to inspect cross-exercise similarities.</p>');

		if (Array.isArray(data.exercises_failed) && data.exercises_failed.length > 0) {
			html.push('<article class="warning">');
			html.push('<strong>' + data.exercises_failed.length + ' exercise(s) failed to generate:</strong>');
			html.push('<ul>');
			for (var j = 0; j < data.exercises_failed.length; j++) {
				var failed = data.exercises_failed[j] || {};
				html.push('<li>' + escapeHtml(failed.name || failed.key || 'Unknown exercise') + ': ' + escapeHtml(failed.error || 'Unknown error') + '</li>');
			}
			html.push('</ul>');
			html.push('</article>');
		}

		html.push('</div>');
		container.innerHTML = html.join('');
	}

	function showReady(data) {
		var progress = el('hubCourseProgress');
		var text = el('hubCourseProgressText');
		var btn = el('generateCourseBtn');
		if (progress) {
			progress.classList.add('active');
		}
		if (text) {
			text.classList.remove('status-error');
			// Handle multiple report IDs (per-exercise reports)
			if (data.report_ids && data.report_ids.length > 0) {
				var total = data.exercises_processed || data.report_ids.length;
				text.textContent = 'Course report ready (' + total + ' exercise reports generated).';
			} else if (data.message) {
				text.textContent = data.message;
			} else {
				text.textContent = 'Course-wide report ready.';
			}
		}
		setHint(null);
		setCourseMeta(data && data.completed_at);
		setProgressBar(data || null);
		setBusy(false);
		if (btn) {
			btn.textContent = 'Generate Course Report';
		}
		renderReadyReports(data);
		stopPolling();
	}

	function startCourseReportGeneration() {
		var progress = el('hubCourseProgress');
		var text = el('hubCourseProgressText');
		setBusy(true);
		if (progress) {
			progress.classList.add('active');
		}
		if (text) {
			text.classList.remove('status-error');
			text.textContent = 'Queuing course-wide report generation\u2026';
		}
		setHint(null);
		setProgressBar(null);

		fetch(generateUrl, {
			method: 'GET',
			headers: { 'X-Requested-With': 'XMLHttpRequest' }
		})
			.then(function (response) { return response.json(); })
			.then(function (data) {
				if (data.status === 'queued') {
					taskId = data.task_id;
					updateProgressText(data);
					startPolling();
				} else {
					showFailure(data.message);
				}
			})
			.catch(function () {
				showFailure('Failed to reach Radar to start report generation. Try again.');
			});
	}

	function checkCourseReportStatus() {
		if (!taskId) {
			stopPolling();
			return;
		}
		pollAttempts += 1;
		if (pollAttempts > MAX_POLL_ATTEMPTS) {
			showFailure('Gave up waiting for a response after a very long time. Please try again.');
			return;
		}
		fetch(checkUrlTemplate.replace('__TASK__', taskId), {
			headers: { 'X-Requested-With': 'XMLHttpRequest' }
		})
			.then(function (response) { return response.json(); })
			.then(function (data) {
				if (data.status === 'ready') {
					showReady(data);
				} else if (data.status === 'failed') {
					showFailure(data.message);
				} else if (data.status === 'pending') {
					updateProgressText(data);
					setBusy(true);
				}
			})
			.catch(function () {
				// Transient network hiccup: keep polling rather than giving up.
			});
	}

	function restoreStatusAfterRefresh() {
		fetch(checkCurrentUrl, {
			method: 'GET',
			headers: { 'X-Requested-With': 'XMLHttpRequest' }
		})
			.then(function (response) { return response.json(); })
			.then(function (data) {
				var btn = el('generateCourseBtn');
				if (data.status === 'pending' && data.task_id) {
					taskId = data.task_id;
					var progress = el('hubCourseProgress');
					if (progress) {
						progress.classList.add('active');
					}
					updateProgressText(data);
					setBusy(true);
					if (btn) {
						btn.textContent = 'Report In Progress';
					}
					startPolling();
				} else if (data.status === 'ready') {
					setCourseMeta(data.completed_at);
					setBusy(false);
				} else if (data.status === 'failed') {
					showFailure(data.message);
				}
			})
			.catch(function () {
				// No-op: the manual "Generate" button still works even if this
				// best-effort restore call fails.
			});
	}

	var button = el('generateCourseBtn');
	if (button) {
		button.addEventListener('click', startCourseReportGeneration);
	}
	restoreStatusAfterRefresh();
})();
