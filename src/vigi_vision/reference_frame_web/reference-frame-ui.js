const candidateForm = document.querySelector("#candidate-form");
const channelIdInput = document.querySelector("#channel-id");
const referenceTimeInput = document.querySelector("#reference-time");
const generateButton = document.querySelector("#generate-button");
const requestStatus = document.querySelector("#request-status");
const requestError = document.querySelector("#request-error");
const candidateResults = document.querySelector("#candidate-results");

function setRequestState(state, message) {
  requestStatus.dataset.state = state;
  requestStatus.textContent = message;
}

function clearResults() {
  requestError.hidden = true;
  requestError.textContent = "";
  candidateResults.replaceChildren();
}

function renderRequestError() {
  requestError.hidden = false;
  requestError.textContent = "The request could not be completed. Check the channel and recorded time, then try again.";
  setRequestState("error", "Candidate request was not completed.");
}

function appendFact(row, label, value, isMono = false) {
  const fact = document.createElement("div");
  fact.className = "candidate-fact";
  const term = document.createElement("dt");
  term.textContent = label;
  const description = document.createElement("dd");
  description.textContent = value;
  if (isMono) {
    description.className = "mono";
  }
  fact.append(term, description);
  row.append(fact);
}

function renderCandidate(candidate) {
  const row = document.createElement("li");
  row.className = "candidate-row";
  row.dataset.status = candidate.status;
  appendFact(row, "Requested position", `${candidate.offset_seconds}s`, true);
  appendFact(row, "Requested UTC time", candidate.candidate_requested_time_utc, true);

  if (candidate.status === "succeeded") {
    appendFact(row, "Status", `Succeeded, ${candidate.outcome}`);
  } else {
    appendFact(row, "Status", "Failed");
    appendFact(row, `Failure: ${candidate.failure.code}`, candidate.failure.message);
  }
  candidateResults.append(row);
}

function renderCandidateSet(candidateSet) {
  if (!Array.isArray(candidateSet.candidates)) {
    renderRequestError();
    return;
  }

  candidateSet.candidates.forEach(renderCandidate);
  const { created, reused, failed } = candidateSet.summary;
  const succeeded = created + reused;
  if (failed === 0) {
    setRequestState("success", `Completed ${succeeded} candidate requests.`);
  } else if (succeeded === 0) {
    setRequestState("all-failed", "No candidate media was available for this valid request.");
  } else {
    setRequestState("partial", `${succeeded} candidate requests completed; ${failed} failed safely.`);
  }
}

async function submitCandidateRequest(event) {
  event.preventDefault();
  clearResults();

  if (!candidateForm.reportValidity()) {
    setRequestState("error", "Enter a positive channel ID and a reference time.");
    return;
  }

  generateButton.disabled = true;
  candidateForm.setAttribute("aria-busy", "true");
  setRequestState("loading", "Generating candidate requests.");

  try {
    const response = await fetch("/api/v1/reference-frame-candidate-sets", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        channel_id: Number(channelIdInput.value),
        reference_time: referenceTimeInput.value,
      }),
    });
    if (!response.ok) {
      renderRequestError();
      return;
    }
    renderCandidateSet(await response.json());
  } catch {
    renderRequestError();
  } finally {
    generateButton.disabled = false;
    candidateForm.removeAttribute("aria-busy");
  }
}

candidateForm.addEventListener("submit", submitCandidateRequest);
