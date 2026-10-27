"""End-to-end smoke test against the running Compose stack (stdlib only).

Sends the k6 prompts through the public API and waits for terminal states.
The mock is stochastic, so the check is statistical: every job must finish,
prompts with an explicit body must mostly be sent, and prompts the mock
cannot extract correctly must never be sent.
"""

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://localhost:5000"
SENDABLE = [
    "Manda un correo a marta@app.com: Tienes un nuevo mensaje.",
    "Recordatorio por teléfono al 699888777: cita a las 10:00",
]
# The mock answers these with the truncated command as the message.
UNSENDABLE = [
    "Enviar email a juan@example.com diciendo que su paquete está en camino.",
    "Avisar por SMS al 600-111-222 que la reserva ha sido confirmada.",
]
ROUNDS = 5


def call(method: str, path: str, body: object = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(BASE + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def wait_until_ready() -> None:
    for _ in range(60):
        try:
            if call("GET", "/v1/requests/does-not-exist")[0] == 404:
                return
        except OSError:
            pass
        time.sleep(1)
    sys.exit("API did not become ready")


def main() -> int:
    wait_until_ready()
    jobs = []
    for prompt in (SENDABLE + UNSENDABLE) * ROUNDS:
        status, body = call("POST", "/v1/requests", {"user_input": prompt})
        assert status == 201, (status, body)
        status, _ = call("POST", f"/v1/requests/{body['id']}/process")
        assert status in (200, 202), status
        jobs.append((prompt, body["id"]))

    outcome: dict[str, str] = {}
    deadline = time.monotonic() + 90
    while len(outcome) < len(jobs) and time.monotonic() < deadline:
        for _, job_id in jobs:
            if job_id not in outcome:
                state = call("GET", f"/v1/requests/{job_id}")[1]["status"]
                if state in ("sent", "failed"):
                    outcome[job_id] = state
        time.sleep(0.5)

    sent = {prompt: sum(outcome.get(job_id) == "sent" for p, job_id in jobs if p == prompt)
            for prompt in SENDABLE + UNSENDABLE}
    print(json.dumps({"finished": len(outcome), "total": len(jobs), "sent_per_prompt": sent},
                     ensure_ascii=False, indent=2))
    if len(outcome) != len(jobs):
        print("FAIL: some jobs did not reach a terminal state")
        return 1
    if any(sent[prompt] for prompt in UNSENDABLE):
        print("FAIL: a request the mock cannot extract correctly was sent")
        return 1
    # With one re-ask the mock refuses twice in a row ~1% of the time.
    if sum(sent[prompt] for prompt in SENDABLE) < len(SENDABLE) * ROUNDS - 2:
        print("FAIL: too few sendable requests were delivered")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
